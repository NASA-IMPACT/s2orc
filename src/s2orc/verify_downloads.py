#!/usr/bin/env python3

import json
import os
import re
import argparse
from pathlib import Path


def extract_shard_id(url):
    match = re.match(r"https://ai2-s2ag\.s3\.amazonaws\.com/staging/(.*)/s2orc(?:_v2)?/(.*?)\.gz(?:\?.*)?", url)
    if match:
        return match.group(2)
    return None


def parse_args():
    parser = argparse.ArgumentParser(description="Verify S2ORC dataset downloads")
    parser.add_argument("--local-path", default="data/s2orc_papers", help="Local path where files were downloaded (default: data/s2orc_papers)")
    return parser.parse_args()


def verify_downloads(local_path):
    metadata_path = f"{local_path}_metadata"
    download_links_file = os.path.join(metadata_path, "download_links.json")

    if not os.path.exists(metadata_path):
        print(f"Error: Metadata directory '{metadata_path}' not found!")
        print(f"Please ensure you have run the download script with --local-path={local_path}")
        return False

    if not os.path.exists(download_links_file):
        print(f"Error: {download_links_file} not found!")
        print(f"Please ensure download_links.json is placed in the metadata folder: {metadata_path}")
        return False

    with open(download_links_file, 'r') as f:
        data = json.load(f)

    total_files = len(data.get('files', []))
    print(f"Expected files: {total_files}")
    print(f"Verifying downloads in: {os.path.abspath(local_path)}\n")

    missing_files = []
    corrupted_files = []
    valid_files = []

    for url in data.get('files', []):
        shard_id = extract_shard_id(url)

        if not shard_id:
            print(f"Warning: Could not extract shard ID from URL: {url[:100]}...")
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


if __name__ == "__main__":
    args = parse_args()
    verify_downloads(args.local_path)
