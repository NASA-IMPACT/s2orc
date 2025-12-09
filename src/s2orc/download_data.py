import json
import os
import re
import requests
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import time
import hashlib

DATASET_NAME = "s2orc"
MAX_RETRIES = 3
CHUNK_SIZE = 8192

def parse_args():
    parser = argparse.ArgumentParser(description="Download S2ORC dataset files")
    parser.add_argument("--api-key", required=True, help="Semantic Scholar API key")
    parser.add_argument("--local-path", default="data/s2orc_papers", help="Local path to save downloaded files (default: data/s2orc_papers)")
    parser.add_argument("--max-workers", type=int, default=10, help="Maximum number of parallel download workers (default: 10)")
    parser.add_argument("--skip-verification", action="store_true", help="Skip verification step after download")
    return parser.parse_args()


def extract_shard_id(url):
    match = re.match(r"https://ai2-s2ag\.s3\.amazonaws\.com/staging/(.*)/s2orc(?:_v2)?/(.*?)\.gz(?:\?.*)?", url)
    if match:
        release_id = match.group(1)
        shard_id = match.group(2)
        return release_id, shard_id
    return None, None


def download_file(url, local_path, max_retries=MAX_RETRIES):
    release_id, shard_id = extract_shard_id(url)

    if not shard_id:
        return {"url": url, "status": "failed", "error": "Failed to extract shard ID from URL"}

    output_path = os.path.join(local_path, f"{shard_id}.gz")

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return {"url": url, "status": "skipped", "shard_id": shard_id, "path": output_path}

    for attempt in range(max_retries):
        try:
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()

            temp_path = output_path + ".tmp"

            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)

            if os.path.getsize(temp_path) == 0:
                raise Exception("Downloaded file is empty")

            os.rename(temp_path, output_path)

            return {"url": url, "status": "success", "shard_id": shard_id, "path": output_path}

        except Exception as e:
            error_msg = f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}"

            temp_path = output_path + ".tmp"
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except:
                    pass

            if attempt == max_retries - 1:
                return {"url": url, "status": "failed", "shard_id": shard_id, "error": error_msg}

            time.sleep(2 ** attempt)

    return {"url": url, "status": "failed", "shard_id": shard_id, "error": "Max retries exceeded"}


def verify_downloads(local_path, metadata_path):
    download_links_file = os.path.join(metadata_path, "download_links.json")

    if not os.path.exists(download_links_file):
        print(f"Error: {download_links_file} not found!")
        return False

    with open(download_links_file, 'r') as f:
        data = json.load(f)

    total_files = len(data.get('files', []))
    print(f"\n{'='*60}")
    print(f"Verification Step")
    print(f"{'='*60}")
    print(f"Expected files: {total_files}")
    print(f"Verifying downloads in: {os.path.abspath(local_path)}\n")

    missing_files = []
    corrupted_files = []
    valid_files = []

    for url in data.get('files', []):
        _, shard_id = extract_shard_id(url)

        if not shard_id:
            missing_files.append({"url": url, "reason": "Failed to extract shard ID"})
            continue

        file_path = os.path.join(local_path, f"{shard_id}.gz")

        if not os.path.exists(file_path):
            missing_files.append({"shard_id": shard_id, "url": url, "reason": "File not found"})
            continue

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            corrupted_files.append({"shard_id": shard_id, "path": file_path, "reason": "File is empty"})
            continue

        valid_files.append({"shard_id": shard_id, "path": file_path, "size": file_size})

    print(f"{'='*60}")
    print(f"Verification Summary:")
    print(f"  Valid files: {len(valid_files)}")
    print(f"  Missing files: {len(missing_files)}")
    print(f"  Corrupted files: {len(corrupted_files)}")
    print(f"  Total expected: {total_files}")
    print(f"{'='*60}\n")

    if missing_files:
        print(f"\nMissing files ({len(missing_files)}):")
        for item in missing_files[:10]:
            shard_id = item.get('shard_id', 'unknown')
            reason = item.get('reason', 'unknown')
            print(f"  - {shard_id}: {reason}")
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more")

        missing_file = os.path.join(metadata_path, "missing_downloads.json")
        with open(missing_file, 'w') as f:
            json.dump(missing_files, f, indent=4)
        print(f"\nFull list saved to: {missing_file}")

    if corrupted_files:
        print(f"\nCorrupted files ({len(corrupted_files)}):")
        for item in corrupted_files[:10]:
            print(f"  - {item['shard_id']}: {item['reason']}")
        if len(corrupted_files) > 10:
            print(f"  ... and {len(corrupted_files) - 10} more")

        corrupted_file = os.path.join(metadata_path, "corrupted_downloads.json")
        with open(corrupted_file, 'w') as f:
            json.dump(corrupted_files, f, indent=4)
        print(f"\nFull list saved to: {corrupted_file}")

        print("\nCleaning up corrupted files...")
        for item in corrupted_files:
            try:
                os.remove(item['path'])
                print(f"  Removed: {item['shard_id']}")
            except Exception as e:
                print(f"  Failed to remove {item['shard_id']}: {e}")

    success_rate = (len(valid_files) / total_files * 100) if total_files > 0 else 0
    total_size = sum(f['size'] for f in valid_files)
    total_size_gb = total_size / (1024 ** 3)

    print(f"\nStatistics:")
    print(f"  Success rate: {success_rate:.2f}%")
    print(f"  Total size of downloaded files: {total_size_gb:.2f} GB")

    all_good = len(missing_files) == 0 and len(corrupted_files) == 0

    if all_good:
        print(f"\nAll files downloaded successfully!")
    else:
        print(f"\nSome files are missing or corrupted. Please re-run the download script.")

    return all_good


def main():
    args = parse_args()

    api_key = args.api_key
    local_path = args.local_path
    max_workers = args.max_workers
    skip_verification = args.skip_verification

    os.makedirs(local_path, exist_ok=True)
    metadata_path = f"{local_path}_metadata"
    os.makedirs(metadata_path, exist_ok=True)

    response = requests.get("https://api.semanticscholar.org/datasets/v1/release/latest").json()
    release_id = response["release_id"]
    print(f"Latest release ID: {release_id}")

    response = requests.get(f"https://api.semanticscholar.org/datasets/v1/release/{release_id}/dataset/{DATASET_NAME}/", headers={"x-api-key": api_key}).json()

    with open(os.path.join(metadata_path, "download_links.json"), "w") as f:
        json.dump(response, f, indent=4)

    print(f"\nStarting parallel download of {len(response['files'])} files with {max_workers} workers...")

    failed_downloads = []
    successful_downloads = []
    skipped_downloads = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(download_file, url, local_path): url for url in response["files"]}

        with tqdm(total=len(response["files"]), desc="Downloading files") as pbar:
            for future in as_completed(future_to_url):
                result = future.result()
                pbar.update(1)

                if result["status"] == "success":
                    successful_downloads.append(result)
                elif result["status"] == "skipped":
                    skipped_downloads.append(result)
                else:
                    failed_downloads.append(result)
                    print(f"\nFailed to download {result.get('shard_id', 'unknown')}: {result.get('error', 'Unknown error')}")

    print(f"\n{'='*60}")
    print(f"Download Summary:")
    print(f"  Successful: {len(successful_downloads)}")
    print(f"  Skipped (already exists): {len(skipped_downloads)}")
    print(f"  Failed: {len(failed_downloads)}")
    print(f"  Total: {len(response['files'])}")
    print(f"{'='*60}")

    if failed_downloads:
        failed_file = os.path.join(metadata_path, "failed_downloads.json")
        with open(failed_file, "w") as f:
            json.dump(failed_downloads, f, indent=4)
        print(f"\nFailed downloads saved to: {failed_file}")

    success_file = os.path.join(metadata_path, "successful_downloads.json")
    with open(success_file, "w") as f:
        json.dump(successful_downloads + skipped_downloads, f, indent=4)
    print(f"Successful downloads logged to: {success_file}")

    print("\nDownload complete!")

    if not skip_verification:
        verify_downloads(local_path, metadata_path)


if __name__ == "__main__":
    main()
