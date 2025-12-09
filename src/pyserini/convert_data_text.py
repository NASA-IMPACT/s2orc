import os
import argparse
import ujson as json
import glob
from pathlib import Path
from multiprocessing import Pool, cpu_count
from tqdm import tqdm


def parse_args():
    parser = argparse.ArgumentParser(description="Convert S2ORC data to Pyserini format (text content)")
    parser.add_argument("--input-path", default="data/s2orc_papers", help="Path to downloaded S2ORC files (default: data/s2orc_papers)")
    parser.add_argument("--output-path", default="data/s2orc_pyserini_text", help="Output path for converted files (default: data/s2orc_pyserini_text)")
    parser.add_argument("--num-workers", type=int, default=8, help="Number of parallel workers (default: 8)")
    return parser.parse_args()

def get_version(json_data):
    version = "v1" if json_data.get("body", {}).get("text","") else "v2"
    return version

def extract_text_from_v1(json_data):
    text = json_data.get("body", {}).get("text","")
    return text

def extract_text_from_v2(json_data):
    text = json_data.get("content",{}).get("text", "")
    return text

def extract_text(json_data):
    text = extract_text_from_v1(json_data) or extract_text_from_v2(json_data)
    return text

def read_jsonl_file(file_path):
    with open(file_path, 'rb', buffering=1024*1024*8) as f:
        offset = 0
        while True:
            line = f.readline()
            if not line:
                break

            line_start_offset = offset
            offset += len(line)

            line_str = line.decode('utf-8').strip()
            if line_str:
                try:
                    json_data = json.loads(line_str)
                    yield json_data, line_start_offset
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON at offset {line_start_offset}: {e}")


def process_single_file(args_tuple):
    input_path, output_dir = args_tuple
    input_filename = Path(input_path).name
    output_path = Path(output_dir) / input_filename

    doc_count = 0
    error_count = 0
    buffer = []
    buffer_size = 50000

    try:
        with open(output_path, 'w', buffering=1024*1024*8) as out_file:
            for json_data, offset in read_jsonl_file(input_path):
                try:
                    text = extract_text(json_data)
                    version = get_version(json_data)

                    doc_id = f"{input_filename}___{offset}__{version}"

                    pyserini_doc = {
                        "id": doc_id,
                        "contents": text
                    }

                    buffer.append(json.dumps(pyserini_doc) + '\n')
                    doc_count += 1

                    if len(buffer) >= buffer_size:
                        out_file.write(''.join(buffer))
                        buffer.clear()

                except Exception as e:
                    error_count += 1
                    print(f"Error processing document at offset {offset} in {input_filename}: {e}")

            if buffer:
                out_file.write(''.join(buffer))

        return (input_filename, doc_count, error_count, str(output_path))

    except Exception as e:
        print(f"Error processing file {input_filename}: {e}")
        return (input_filename, 0, 0, None)


def process_all_files_parallel(input_files, output_dir, num_workers=None):
    if num_workers is None:
        num_workers = cpu_count()

    print(f"Processing {len(input_files)} files using {num_workers} workers...")

    args_list = [(f, output_dir) for f in input_files]

    with Pool(num_workers) as pool:
        results = list(tqdm(
            pool.imap(process_single_file, args_list),
            total=len(input_files),
            desc="Processing files"
        ))

    total_docs = sum(r[1] for r in results)
    total_errors = sum(r[2] for r in results)
    successful_files = sum(1 for r in results if r[3] is not None)

    print(f"\n{'='*60}")
    print(f"Processing complete!")
    print(f"Files processed: {successful_files}/{len(input_files)}")
    print(f"Total documents: {total_docs:,}")
    print(f"Total errors: {total_errors:,}")
    print(f"Output directory: {output_dir}")
    print(f"{'='*60}")

    return results


if __name__ == "__main__":
    args = parse_args()

    input_path = Path(args.input_path)
    output_path = Path(args.output_path)
    num_workers = args.num_workers

    output_path.mkdir(exist_ok=True, parents=True)

    input_files = glob.glob(str(input_path / "*"))

    if not input_files:
        print(f"Error: No files found in {input_path}")
        exit(1)

    results = process_all_files_parallel(input_files, str(output_path), num_workers=num_workers)
