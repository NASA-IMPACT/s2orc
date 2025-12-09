## S2ORC Dataset Download and Verification

### Prerequisites

Install required Python packages:

```bash
pip install requests tqdm
```

For faster decompression, install pigz:

```bash
# Ubuntu/Debian
sudo apt-get install pigz

# macOS
brew install pigz

# RHEL/CentOS/Fedora
sudo yum install pigz
```

If pigz is not available, the decompression scripts will fall back to using standard gunzip (slower).

### Step 1: Download the Dataset

The download script fetches S2ORC dataset files from Semantic Scholar and automatically verifies the downloads.

#### Basic Usage

```bash
python src/s2orc/download_data.py --api-key YOUR_API_KEY
```

#### Command-line Arguments

- `--api-key` (required): Your Semantic Scholar API key
- `--local-path` (optional, default: `s2orc_papers`): Directory where files will be saved
- `--max-workers` (optional, default: `10`): Number of parallel download workers
- `--skip-verification` (optional): Skip the automatic verification step after download

#### Examples

Download with default settings:
```bash
python src/s2orc/download_data.py --api-key YOUR_API_KEY
```

Download to custom directory with more workers:
```bash
python src/s2orc/download_data.py --api-key YOUR_API_KEY --local-path s2orc_dataset --max-workers 20
```

Download without automatic verification:
```bash
python src/s2orc/download_data.py --api-key YOUR_API_KEY --skip-verification
```

#### Output Structure

The script creates two directories:

- `{local_path}/` - Contains all downloaded `.gz` files
- `{local_path}_metadata/` - Contains metadata files:
  - `download_links.json` - List of all download URLs
  - `successful_downloads.json` - Log of successfully downloaded files
  - `failed_downloads.json` - Log of failed downloads (if any)
  - `missing_downloads.json` - List of missing files (if verification finds any)
  - `corrupted_downloads.json` - List of corrupted files (if verification finds any)

### Step 2: Verify Downloads (Optional)

The download script automatically runs verification after download completes. However, you can also run verification separately:

```bash
python src/s2orc/verify_downloads.py --local-path s2orc_papers
```

The verification script will:
- Check that all files from `download_links.json` exist
- Verify that files are not empty or corrupted
- Clean up any corrupted files automatically
- Generate detailed reports saved to the metadata directory
- Display statistics including success rate and total data size

### Step 3: Decompress Files

After downloading, you need to decompress the `.gz` files to `.jsonl` format. Two scripts are provided:

#### Sequential Decompression (Safer for Limited Disk Space)

Processes one file at a time to avoid running out of disk space:

```bash
bash src/s2orc/decompress_sequential.sh s2orc_papers [threads]
```

**Arguments:**
- `data_directory` (required): Path to directory containing `.gz` files
- `threads` (optional, default: 30): Number of threads for pigz

**Features:**
- Processes files one at a time
- Shows available disk space before each file
- Warns if disk space is low (<100GB)
- Uses pigz if available for faster decompression
- Automatically adds `.jsonl` extension to decompressed files

#### Parallel Decompression (Faster but Requires More Disk Space)

Processes multiple files simultaneously:

```bash
bash src/s2orc/decompress_parallel.sh s2orc_papers [parallel_jobs]
```

**Arguments:**
- `data_directory` (required): Path to directory containing `.gz` files
- `parallel_jobs` (optional, default: 30): Number of files to decompress in parallel

**Features:**
- Processes multiple files in parallel for faster decompression
- Automatically adds `.jsonl` extension to decompressed files
- Deletes `.gz` files after successful decompression

**Note:** The parallel version requires significantly more disk space as it decompresses multiple files simultaneously. Use the sequential version if disk space is limited.

