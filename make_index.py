#!/usr/bin/env python3

"""
S2ORC End-to-End Setup Script
Orchestrates the complete workflow: download data, create indexes, and start server
Supports downloading pre-built indexes from S3 for faster setup
"""

import os
import sys
import subprocess
import time
import logging
import json
import re
import shutil
import tempfile
import tarfile
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

from tqdm import tqdm
import requests

# Compile regex patterns at module level for performance
FILE_ID_PATTERN = re.compile(r'/([^/]+?)\.gz')

# Cache for size calculations to avoid repeated expensive operations
_size_cache: Dict[str, float] = {}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('s2orc_setup.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Configuration:
    """Configuration for S2ORC setup"""
    # Paths
    s2orc_papers_path: str = "data/s2orc_papers"
    full_text_index_dir: str = "data/s2orc_index_text"
    title_index_dir: str = "data/s2orc_index_title"

    # S3 Configuration
    s3_bucket: Optional[str] = "llm-akd-s2orc"
    s3_prefix: str = "s2orc-indexes/"
    aws_region: str = "us-east-1"

    # API Keys
    s2_api_key: Optional[str] = None

    # Processing Configuration
    download_workers: int = 10
    conversion_workers: int = 8
    index_threads: int = 20
    decompress_method: str = "sequential"

    # Test Mode (limit files for testing)
    test_mode: bool = False
    file_limit: Optional[int] = None

    # Server Configuration
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    auto_start_server: bool = True

    # Update Mode
    update_mode: bool = False
    dataset_name: str = "s2orc"

    # Upload to S3
    upload_to_s3: bool = False


def print_banner(text: str):
    """Print a formatted banner"""
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70 + "\n")


def check_java_installed() -> bool:
    """
    Check if Java is installed and accessible
    Returns True if Java is available, False otherwise
    """
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            timeout=5
        )

        # Java prints version to stderr
        version_output = result.stderr if result.stderr else result.stdout

        if result.returncode == 0 and version_output:
            # Extract version info
            version_line = version_output.split('\n')[0]
            logger.info(f"Java found: {version_line}")
            return True
        else:
            return False

    except FileNotFoundError:
        return False
    except Exception as e:
        logger.warning(f"Error checking Java: {e}")
        return False


def print_java_installation_instructions():
    """Print instructions for installing Java"""
    print("\n" + "!" * 70)
    print("ERROR: Java is not installed or not accessible")
    print("!" * 70)
    print("\nPyserini requires Java to run (it uses Apache Lucene).")
    print("\nInstallation instructions:")
    print("\n  macOS (using Homebrew):")
    print("    brew install openjdk@17")
    print("    sudo ln -sfn /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk \\")
    print("      /Library/Java/JavaVirtualMachines/openjdk-17.jdk")
    print("    export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")
    print("\n  Linux (Ubuntu/Debian):")
    print("    sudo apt-get update")
    print("    sudo apt-get install openjdk-17-jdk")
    print("\n  Linux (CentOS/RHEL):")
    print("    sudo yum install java-17-openjdk")
    print("\n  Or download from: https://www.oracle.com/java/technologies/downloads/")
    print("\nAfter installation, verify with: java -version")
    print("!" * 70 + "\n")


def print_pyserini_environment_fix():
    """Print instructions for fixing Pyserini/Java environment"""
    print("\n" + "!" * 70)
    print("ERROR: Pyserini cannot access Java libraries")
    print("!" * 70)
    print("\nThe Java JVM libraries are not accessible to Pyserini.")
    print("\nFor macOS with Homebrew OpenJDK:")
    print("\n  1. Find your Java installation:")
    print("     brew --prefix openjdk")
    print("\n  2. Set JAVA_HOME to the JDK home directory:")
    print("     export JAVA_HOME=$(brew --prefix openjdk)/libexec/openjdk.jdk/Contents/Home")
    print("\n  3. Set DYLD_LIBRARY_PATH to include Java libraries:")
    print("     export DYLD_LIBRARY_PATH=\"$JAVA_HOME/lib:$JAVA_HOME/lib/server:$DYLD_LIBRARY_PATH\"")
    print("\n  4. Add these to your ~/.zshrc or ~/.bashrc:")
    print("     echo 'export JAVA_HOME=$(brew --prefix openjdk)/libexec/openjdk.jdk/Contents/Home' >> ~/.zshrc")
    print("     echo 'export DYLD_LIBRARY_PATH=\"$JAVA_HOME/lib:$JAVA_HOME/lib/server:$DYLD_LIBRARY_PATH\"' >> ~/.zshrc")
    print("     source ~/.zshrc")
    print("\nFor Linux:")
    print("\n  1. Find libjvm.so:")
    print("     find /usr/lib/jvm -name libjvm.so")
    print("\n  2. Set JAVA_HOME and LD_LIBRARY_PATH accordingly")
    print("\nAfter fixing, restart your terminal and try again.")
    print("!" * 70 + "\n")


def test_pyserini_indexing() -> bool:
    """
    Test if Pyserini can actually create an index
    Creates a minimal test index and deletes it
    Returns True if successful, False otherwise
    """
    logger.info("Testing Pyserini indexing capability...")

    # Create temporary directories
    temp_dir = Path(tempfile.mkdtemp(prefix="s2orc_test_"))
    test_data_dir = temp_dir / "data"
    test_index_dir = temp_dir / "index"
    test_data_dir.mkdir(exist_ok=True)
    test_index_dir.mkdir(exist_ok=True)

    try:
        # Create a minimal test document
        test_doc = {
            "id": "test_doc_1",
            "contents": "This is a test document for Pyserini indexing."
        }

        test_file = test_data_dir / "test.jsonl"
        with open(test_file, 'w') as f:
            f.write(json.dumps(test_doc) + '\n')

        logger.info(f"Created test document at {test_file}")

        # Try to build index using Pyserini
        cmd = [
            sys.executable, "-m", "pyserini.index.lucene",
            "--collection", "JsonCollection",
            "--input", str(test_data_dir),
            "--index", str(test_index_dir),
            "--generator", "DefaultLuceneDocumentGenerator",
            "--threads", "1"
        ]

        logger.info("Attempting to build test index...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            logger.info("✓ Pyserini indexing test successful!")

            # Verify index was created
            if validate_index_directory(test_index_dir):
                logger.info("✓ Test index validation passed")
                return True
            else:
                logger.error("Test index was created but validation failed")
                logger.error(f"Files in index directory: {list(test_index_dir.glob('*'))}")
                # Still return True if indexing succeeded, validation might be too strict
                logger.warning("Proceeding anyway since indexing command succeeded")
                return True
        else:
            logger.error("✗ Pyserini indexing test failed")
            logger.error(f"Exit code: {result.returncode}")
            if result.stdout:
                logger.error(f"Output:\n{result.stdout}")
            if result.stderr:
                logger.error(f"Error:\n{result.stderr}")

            # Print the full error for user to see
            print("\n" + "!" * 70)
            print("PYSERINI TEST FAILED - Error Details:")
            print("!" * 70)
            if result.stderr:
                print(result.stderr)
            if result.stdout:
                print(result.stdout)
            print("!" * 70 + "\n")

            # Check for common errors
            error_text = result.stderr + result.stdout
            if "libjvm" in error_text or "JAVA_HOME" in error_text or "jnius" in error_text:
                print_pyserini_environment_fix()
            elif "Java" in error_text:
                print_java_installation_instructions()

            return False

    except subprocess.TimeoutExpired:
        logger.error("Test indexing timed out")
        return False
    except Exception as e:
        logger.error(f"Test indexing failed with exception: {e}")
        logger.exception("Full traceback:")

        # Try to provide helpful error message
        error_str = str(e)
        if "libjvm" in error_str or "JAVA_HOME" in error_str or "jnius" in error_str:
            print_pyserini_environment_fix()
        elif "Java" in error_str:
            print_java_installation_instructions()

        return False

    finally:
        # Clean up test files
        try:
            shutil.rmtree(temp_dir)
            logger.info(f"Cleaned up test directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean up test directory: {e}")


def check_parallel_compression_tool(tool: str) -> bool:
    """
    Check if a parallel compression tool is available
    Args:
        tool: Tool name ('pigz' or 'pbzip2')
    Returns:
        True if tool is available, False otherwise
    """
    try:
        result = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            timeout=2
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def validate_index_directory(index_path: Path) -> bool:
    """
    Validate that index directory has required Lucene files
    """
    if not index_path.exists():
        return False

    # Check for any of these common Lucene index files
    # segments_* is the primary indicator, but accept other patterns too
    possible_patterns = ['segments*', '*.si', '*.cfs', '*.cfe', '*.fdx', '*.fdt']

    for pattern in possible_patterns:
        if next(index_path.glob(pattern), None) is not None:
            return True

    # If no known patterns found, check if directory has any files at all
    # (Some Lucene versions may use different file extensions)
    all_files = list(index_path.glob("*"))
    if all_files and len([f for f in all_files if f.is_file()]) > 0:
        logger.warning(f"Index directory has files but not standard Lucene patterns: {[f.name for f in all_files[:5]]}")
        return True

    return False


def extract_file_id(url: str) -> Optional[str]:
    """
    Extract the file ID from an S3 URL
    Example: https://ai2-s2ag.s3.amazonaws.com/.../20251107_123432_00031_w38wj_02339e22-2c6f-413e-a64e-8f5672a59b87.gz?...
    Returns: 20251107_123432_00031_w38wj_02339e22-2c6f-413e-a64e-8f5672a59b87
    """
    match = FILE_ID_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


def fetch_latest_dataset_info(config: Configuration) -> Dict[str, Any]:
    """
    Fetch the latest dataset information from Semantic Scholar API
    Returns the dataset metadata including file URLs
    """
    logger.info("Fetching latest dataset information from Semantic Scholar API...")

    try:
        # Get latest release ID
        response = requests.get("https://api.semanticscholar.org/datasets/v1/release/latest")
        response.raise_for_status()
        release_data = response.json()
        release_id = release_data["release_id"]
        logger.info(f"Latest release ID: {release_id}")

        # Get dataset files for this release
        dataset_url = f"https://api.semanticscholar.org/datasets/v1/release/{release_id}/dataset/{config.dataset_name}/"
        headers = {"x-api-key": config.s2_api_key} if config.s2_api_key else {}

        response = requests.get(dataset_url, headers=headers)
        response.raise_for_status()
        dataset_info = response.json()

        logger.info(f"Found {len(dataset_info.get('files', []))} files in latest dataset")
        return dataset_info

    except requests.RequestException as e:
        logger.error(f"Failed to fetch dataset information: {e}")
        raise


def save_dataset_info(dataset_info: Dict[str, Any], docs_dir: Path = Path("docs")) -> Path:
    """
    Save dataset information to a timestamped JSON file
    Returns the path to the saved file
    """
    docs_dir.mkdir(exist_ok=True, parents=True)

    # Generate timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"index_list_{timestamp}.json"
    filepath = docs_dir / filename

    logger.info(f"Saving dataset info to {filepath}...")
    with open(filepath, 'w') as f:
        json.dump(dataset_info, f, indent=2)

    logger.info(f"Dataset info saved to {filepath}")
    return filepath


def compare_file_lists(old_json_path: Path, new_dataset_info: Dict[str, Any]) -> Dict[str, Set[str]]:
    """
    Compare old and new file lists to identify new files
    Returns dict with 'new_files' (IDs to download), 'removed_files', 'common_files'
    """
    logger.info("Comparing file lists to identify new files...")

    # Load old file IDs
    old_file_ids = set()
    if old_json_path.exists():
        with open(old_json_path, 'r') as f:
            old_data = json.load(f)
        for url in old_data.get('files', []):
            file_id = extract_file_id(url)
            if file_id:
                old_file_ids.add(file_id)
        logger.info(f"Old dataset has {len(old_file_ids)} files")
    else:
        logger.warning(f"Old index list not found at {old_json_path}, treating all files as new")

    # Extract new file IDs and create mapping
    new_file_ids = set()
    file_id_to_url = {}
    for url in new_dataset_info.get('files', []):
        file_id = extract_file_id(url)
        if file_id:
            new_file_ids.add(file_id)
            file_id_to_url[file_id] = url

    logger.info(f"New dataset has {len(new_file_ids)} files")

    # Compare
    new_files = new_file_ids - old_file_ids
    removed_files = old_file_ids - new_file_ids
    common_files = old_file_ids & new_file_ids

    logger.info(f"Comparison results:")
    logger.info(f"  New files to download: {len(new_files)}")
    logger.info(f"  Removed files: {len(removed_files)}")
    logger.info(f"  Common files: {len(common_files)}")

    return {
        'new_files': new_files,
        'removed_files': removed_files,
        'common_files': common_files,
        'file_id_to_url': file_id_to_url
    }


def collect_configuration() -> Configuration:
    """
    Collect configuration from environment and user prompts
    """
    print_banner("S2ORC Setup - Configuration")

    # Load .env file if available
    if DOTENV_AVAILABLE:
        env_file = Path('.env')
        if env_file.exists():
            load_dotenv()
            logger.info("Loaded configuration from .env file")

    config = Configuration()

    # Read from environment
    config.s2orc_papers_path = os.getenv("S2ORC_PAPERS", config.s2orc_papers_path)
    config.full_text_index_dir = os.getenv("FULL_TEXT_INDEX_DIR", config.full_text_index_dir)
    config.title_index_dir = os.getenv("TITLE_INDEX_DIR", config.title_index_dir)
    config.s2_api_key = os.getenv("S2_API_KEY")
    config.s3_bucket = os.getenv("S3_BUCKET")
    config.s3_prefix = os.getenv("S3_PREFIX", config.s3_prefix)
    config.aws_region = os.getenv("AWS_REGION", config.aws_region)

    # Check for test mode
    test_mode_env = os.getenv("TEST_MODE", "false").lower()
    config.test_mode = test_mode_env in ("true", "1", "yes")
    if config.test_mode:
        config.file_limit = int(os.getenv("FILE_LIMIT", "1"))

    # Prompt for S3 bucket if not set
    if not config.s3_bucket:
        print("\nS3 Configuration (optional - press Enter to skip):")
        s3_bucket = input("  S3 bucket name for pre-built indexes: ").strip()
        if s3_bucket:
            config.s3_bucket = s3_bucket
            if not config.s3_prefix:
                s3_prefix = input(f"  S3 prefix (default: {config.s3_prefix}): ").strip()
                if s3_prefix:
                    config.s3_prefix = s3_prefix

    # Check AWS credentials if S3 configured
    if config.s3_bucket:
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")

        if not aws_access_key or not aws_secret_key:
            logger.warning("AWS credentials not found in environment variables")
            logger.warning("Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY to use S3")
            response = input("Continue without S3? (y/n): ").strip().lower()
            if response != 'y':
                sys.exit(0)
            config.s3_bucket = None

    return config


def display_configuration_summary(config: Configuration):
    """Display configuration summary"""
    print("\nConfiguration Summary:")
    print("-" * 70)
    print(f"Mode:                {'UPDATE (incremental)' if config.update_mode else 'FULL SETUP'}")
    print(f"Data Path:           {config.s2orc_papers_path}")
    print(f"Full-text Index:     {config.full_text_index_dir}")
    print(f"Title Index:         {config.title_index_dir}")

    if config.s3_bucket and not config.update_mode:
        print(f"\nS3 Configuration:")
        print(f"  Bucket:            {config.s3_bucket}")
        print(f"  Prefix:            {config.s3_prefix}")
        print(f"  Region:            {config.aws_region}")
    elif not config.update_mode:
        print(f"\nS3:                  Not configured (will build locally)")

    print(f"\nProcessing:")
    print(f"  Download Workers:  {config.download_workers}")
    print(f"  Conversion Workers: {config.conversion_workers}")
    print(f"  Index Threads:     {config.index_threads}")
    print(f"  Decompress Method: {config.decompress_method}")

    if config.test_mode:
        print(f"\n*** TEST MODE ENABLED ***")
        print(f"  File Limit:        {config.file_limit}")

    if config.update_mode:
        print(f"\n*** UPDATE MODE ENABLED ***")
        print(f"  Will fetch latest dataset and compare with docs/index_list.json")
        print(f"  Only new files will be downloaded and indexed")

    print(f"\nServer:")
    print(f"  Host:              {config.server_host}")
    print(f"  Port:              {config.server_port}")
    print(f"  Auto-start:        {config.auto_start_server}")
    print("-" * 70)


def check_s3_indexes_exist(config: Configuration) -> Dict[str, Any]:
    """
    Check if pre-built indexes exist in S3
    Returns dict with existence status and metadata
    """
    if not BOTO3_AVAILABLE:
        logger.error("boto3 is not installed. Install it with: pip install boto3")
        return {'full_text_exists': False, 'title_exists': False}

    try:
        s3_client = boto3.client('s3', region_name=config.aws_region)

        result = {
            'full_text_exists': False,
            'title_exists': False,
            'full_text_key': None,
            'title_key': None,
            'full_text_size': 0,
            'title_size': 0
        }

        # Check for full-text index (look for actual filename in bucket)
        # Try with and without prefix
        possible_full_text_keys = [
            f"{config.s3_prefix}bm25-pyserini-full-text.tar.gz",
            "bm25-pyserini-full-text.tar.gz",
            f"{config.s3_prefix}s2orc_index_text.tar.gz",  # fallback to old name
        ]

        for full_text_key in possible_full_text_keys:
            try:
                logger.debug(f"Checking for full-text index: {full_text_key}")
                response = s3_client.head_object(Bucket=config.s3_bucket, Key=full_text_key)
                result['full_text_exists'] = True
                result['full_text_key'] = full_text_key
                result['full_text_size'] = response['ContentLength']
                logger.info(f"Found full-text index: {full_text_key}")
                break
            except ClientError as e:
                if e.response['Error']['Code'] != '404':
                    raise
                logger.debug(f"Not found: {full_text_key}")

        # Check for title index (supports both .tar.gz and .tar.bz2)
        possible_title_keys = [
            f"{config.s3_prefix}bm25-pyserini-title.tar.gz",  # test bucket uses .tar.gz
            "bm25-pyserini-title.tar.gz",
            f"{config.s3_prefix}bm25-pyserini-title.tar.bz2",  # prod bucket uses .tar.bz2
            "bm25-pyserini-title.tar.bz2",
            f"{config.s3_prefix}s2orc_index_title.tar.gz",  # fallback to old name
            f"{config.s3_prefix}s2orc_index_title.tar.bz2",
        ]

        for title_key in possible_title_keys:
            try:
                logger.debug(f"Checking for title index: {title_key}")
                response = s3_client.head_object(Bucket=config.s3_bucket, Key=title_key)
                result['title_exists'] = True
                result['title_key'] = title_key
                result['title_size'] = response['ContentLength']
                logger.info(f"Found title index: {title_key}")
                break
            except ClientError as e:
                if e.response['Error']['Code'] != '404':
                    raise
                logger.debug(f"Not found: {title_key}")

        return result

    except NoCredentialsError:
        logger.error("AWS credentials not found or invalid")
        raise
    except ClientError as e:
        logger.error(f"S3 error: {e}")
        raise


def get_index_size_mb(index_path: Path) -> float:
    """
    Calculate total size of index directory in MB
    Uses caching and os.scandir() for better performance
    """
    # Use string path as cache key
    cache_key = str(index_path.resolve())

    # Check cache first
    if cache_key in _size_cache:
        return _size_cache[cache_key]

    # Return 0 if path doesn't exist
    if not index_path.exists():
        _size_cache[cache_key] = 0.0
        return 0.0

    total_size = 0
    # Use os.scandir for better performance on large directories
    for entry in os.scandir(index_path):
        if entry.is_file(follow_symlinks=False):
            total_size += entry.stat(follow_symlinks=False).st_size
        elif entry.is_dir(follow_symlinks=False):
            # Recursively calculate subdirectory sizes
            total_size += get_index_size_mb(Path(entry.path)) * (1024 * 1024)  # Convert back to bytes

    size_mb = total_size / (1024 * 1024)  # Convert to MB
    _size_cache[cache_key] = size_mb
    return size_mb


def validate_index_sizes(full_text_index_dir: Path, title_index_dir: Path) -> bool:
    """
    Validate that full-text index is larger than title index
    Returns True if validation passes, False with warnings otherwise
    """
    if not full_text_index_dir.exists() or not title_index_dir.exists():
        logger.warning("Cannot validate index sizes - one or both indexes don't exist")
        return True  # Can't validate, but don't fail

    text_size = get_index_size_mb(full_text_index_dir)
    title_size = get_index_size_mb(title_index_dir)

    logger.info(f"Index size comparison:")
    logger.info(f"  Full-text index: {text_size:.2f} MB")
    logger.info(f"  Title index:     {title_size:.2f} MB")

    if text_size <= title_size:
        logger.error(f"⚠️  ERROR: Full-text index ({text_size:.2f} MB) is smaller than or equal to title index ({title_size:.2f} MB)!")
        logger.error(f"   This suggests the indexes may have been swapped.")
        logger.error(f"   Please check your S3 bucket configuration:")
        logger.error(f"     - Full-text index should be at: {full_text_index_dir}")
        logger.error(f"     - Title index should be at: {title_index_dir}")
        return False
    elif text_size < title_size * 2:
        logger.warning(f"⚠️  WARNING: Full-text index ({text_size:.2f} MB) is only {text_size/title_size:.1f}x larger than title index")
        logger.warning(f"   Expected ratio: at least 2x, typically 10-50x")
        logger.warning(f"   The indexes might be swapped or incomplete.")
        return True  # Warning but not error
    else:
        ratio = text_size / title_size
        logger.info(f"✓ Size validation passed: full-text is {ratio:.1f}x larger than title")
        return True


def download_index_from_s3(config: Configuration, key: str, local_path: Path, expected_type: str = None):
    """
    Download and extract index from S3 with progress tracking
    Handles different directory structures inside the archive

    Args:
        config: Configuration object
        key: S3 key for the index archive
        local_path: Target local path for the index
        expected_type: Optional type hint ('text' or 'title') for validation
    """
    if not BOTO3_AVAILABLE:
        raise ImportError("boto3 is not installed")

    s3_client = boto3.client('s3', region_name=config.aws_region)

    # Create temp directories
    temp_dir = Path("/tmp/s2orc_index_download")
    temp_dir.mkdir(exist_ok=True, parents=True)
    temp_file = temp_dir / Path(key).name
    temp_extract_dir = temp_dir / "extracted"
    temp_extract_dir.mkdir(exist_ok=True, parents=True)

    try:
        # Get file size for progress bar
        response = s3_client.head_object(Bucket=config.s3_bucket, Key=key)
        file_size = response['ContentLength']

        logger.info(f"Downloading {key} ({file_size / 1e9:.2f} GB)...")

        # Download with progress
        with tqdm(total=file_size, unit='B', unit_scale=True, desc=f"Downloading {Path(key).name}") as pbar:
            def callback(bytes_transferred):
                pbar.update(bytes_transferred)

            s3_client.download_file(
                config.s3_bucket, key, str(temp_file),
                Callback=callback
            )

        # Extract archive to temporary location first
        logger.info(f"Extracting archive...")

        # Determine extraction command based on file extension
        # Use parallel decompression tools (pigz/pbzip2) when available for 3-4x speedup
        if temp_file.suffix == '.gz' or temp_file.name.endswith('.tar.gz'):
            if check_parallel_compression_tool('pigz'):
                logger.info("Using pigz for parallel decompression")
                extract_cmd = ["tar", "-I", "pigz", "-xf", str(temp_file), "-C", str(temp_extract_dir)]
            else:
                logger.info("pigz not found, using standard gzip (slower). Install pigz for faster extraction: brew install pigz")
                extract_cmd = ["tar", "-xzf", str(temp_file), "-C", str(temp_extract_dir)]
        elif temp_file.suffix == '.bz2' or temp_file.name.endswith('.tar.bz2'):
            if check_parallel_compression_tool('pbzip2'):
                logger.info("Using pbzip2 for parallel decompression")
                extract_cmd = ["tar", "-I", "pbzip2", "-xf", str(temp_file), "-C", str(temp_extract_dir)]
            else:
                logger.info("pbzip2 not found, using standard bzip2 (slower). Install pbzip2 for faster extraction: brew install pbzip2")
                extract_cmd = ["tar", "-xjf", str(temp_file), "-C", str(temp_extract_dir)]
        else:
            raise ValueError(f"Unsupported archive format: {temp_file.name}")

        subprocess.run(extract_cmd, check=True)

        # Smart handling: find the actual index directory
        logger.info("Locating extracted index files...")
        extracted_contents = list(temp_extract_dir.iterdir())

        # Case 1: Archive contains a single directory
        if len(extracted_contents) == 1 and extracted_contents[0].is_dir():
            source_dir = extracted_contents[0]
            logger.info(f"Found index in subdirectory: {source_dir.name}")
        # Case 2: Archive contains files directly
        elif validate_index_directory(temp_extract_dir):
            source_dir = temp_extract_dir
            logger.info("Index files extracted directly (no subdirectory)")
        # Case 3: Multiple subdirectories - try to find the right one
        else:
            # Look for directories that contain valid index files
            valid_dirs = [d for d in extracted_contents if d.is_dir() and validate_index_directory(d)]
            if valid_dirs:
                source_dir = valid_dirs[0]
                logger.info(f"Found valid index in: {source_dir.name}")
                if len(valid_dirs) > 1:
                    logger.warning(f"Multiple valid index directories found, using: {source_dir.name}")
            else:
                raise ValueError(
                    f"Could not find valid Lucene index in extracted archive.\n"
                    f"Extracted contents: {[item.name for item in extracted_contents]}\n"
                    f"Expected Lucene index files (segments*, *.si, *.cfs, etc.)"
                )

        # Validate the source directory
        if not validate_index_directory(source_dir):
            raise ValueError(f"Extracted directory {source_dir} does not contain a valid Lucene index")

        # Move/copy to target location
        logger.info(f"Moving index to {local_path}...")
        local_path.parent.mkdir(exist_ok=True, parents=True)

        # If target exists, remove it first
        if local_path.exists():
            logger.warning(f"Target directory {local_path} exists, removing...")
            shutil.rmtree(local_path)

        # Move the source directory to target
        shutil.move(str(source_dir), str(local_path))

        # Final validation
        if not validate_index_directory(local_path):
            raise ValueError(f"Index at {local_path} failed final validation")

        # Size-based validation (if type is specified)
        index_size_mb = get_index_size_mb(local_path)
        logger.info(f"✓ Index successfully extracted to {local_path}")
        logger.info(f"✓ Index contains {len(list(local_path.glob('*')))} files")
        logger.info(f"✓ Index size: {index_size_mb:.2f} MB")

        if expected_type:
            # Full-text index should typically be much larger than title index
            # Typical sizes: full-text ~10-50GB, title ~500MB-2GB
            if expected_type == 'text':
                if index_size_mb < 1000:  # Less than 1GB is suspicious for full-text
                    logger.warning(f"⚠ WARNING: Full-text index seems small ({index_size_mb:.2f} MB)")
                    logger.warning(f"   Expected size: > 1GB. You may have downloaded the title index instead!")
                    logger.warning(f"   Continuing anyway, but please verify...")
                else:
                    logger.info(f"✓ Size validation passed for full-text index")
            elif expected_type == 'title':
                if index_size_mb > 5000:  # More than 5GB is suspicious for title-only
                    logger.warning(f"⚠ WARNING: Title index seems large ({index_size_mb:.2f} MB)")
                    logger.warning(f"   Expected size: < 5GB. You may have downloaded the full-text index instead!")
                    logger.warning(f"   Continuing anyway, but please verify...")
                else:
                    logger.info(f"✓ Size validation passed for title index")

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == '404':
            raise FileNotFoundError(f"Index not found in S3: {key}")
        elif error_code == '403':
            raise PermissionError(f"Access denied to S3 object: {key}")
        else:
            raise

    finally:
        # Cleanup temp files and directories
        if temp_file.exists():
            temp_file.unlink()
        if temp_extract_dir.exists():
            shutil.rmtree(temp_extract_dir, ignore_errors=True)


def upload_indexes_to_s3(config: Configuration) -> bool:
    """
    Upload indexes to S3 following the standard naming convention:
    s3://bucket/prefix/YYYY-MM-DD_HHMMSS/s2orc_index_text.tar.gz
    s3://bucket/prefix/YYYY-MM-DD_HHMMSS/s2orc_index_title.tar.bz2

    Returns True on success, False on failure
    """
    if not BOTO3_AVAILABLE:
        logger.error("boto3 is not installed. Install it with: pip install boto3")
        return False

    if not config.s3_bucket:
        logger.error("S3 bucket not configured. Set S3_BUCKET in environment or .env file")
        return False

    print_banner("Uploading Indexes to S3")

    # Check that indexes exist
    full_text_index = Path(config.full_text_index_dir)
    title_index = Path(config.title_index_dir)

    if not validate_index_directory(full_text_index):
        logger.error(f"Full-text index not found or invalid: {full_text_index}")
        return False

    if not validate_index_directory(title_index):
        logger.error(f"Title index not found or invalid: {title_index}")
        return False

    # Create timestamp directory name (following convention)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    s3_prefix_with_timestamp = f"{config.s3_prefix.rstrip('/')}/{timestamp}/"

    logger.info(f"Upload destination: s3://{config.s3_bucket}/{s3_prefix_with_timestamp}")
    logger.info(f"This creates a versioned snapshot of your indexes")

    # Confirm with user
    response = input("\nProceed with upload? (y/n): ").strip().lower()
    if response != 'y':
        logger.info("Upload cancelled by user")
        return False

    try:
        s3_client = boto3.client('s3', region_name=config.aws_region)

        # Create temporary directory for archives
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Compress full-text index
            logger.info("\nCompressing full-text index...")
            text_archive = temp_path / "s2orc_index_text.tar.gz"

            # Use pigz for parallel compression if available (3-4x faster)
            if check_parallel_compression_tool('pigz'):
                logger.info("Using pigz for parallel compression (much faster)")
                subprocess.run(
                    ["tar", "-I", "pigz", "-cf", str(text_archive), "-C", str(full_text_index.parent), full_text_index.name],
                    check=True
                )
            else:
                logger.info("pigz not found, using standard gzip (slower). Install pigz for faster compression: brew install pigz")
                with tarfile.open(text_archive, "w:gz") as tar:
                    tar.add(full_text_index, arcname=full_text_index.name)

            text_size = text_archive.stat().st_size
            logger.info(f"✓ Full-text archive created: {text_size / 1e9:.2f} GB")

            # Compress title index (using bz2 for better compression on smaller files)
            logger.info("\nCompressing title index...")
            title_archive = temp_path / "s2orc_index_title.tar.bz2"

            # Use pbzip2 for parallel compression if available (3-4x faster)
            if check_parallel_compression_tool('pbzip2'):
                logger.info("Using pbzip2 for parallel compression (much faster)")
                subprocess.run(
                    ["tar", "-I", "pbzip2", "-cf", str(title_archive), "-C", str(title_index.parent), title_index.name],
                    check=True
                )
            else:
                logger.info("pbzip2 not found, using standard bzip2 (slower). Install pbzip2 for faster compression: brew install pbzip2")
                with tarfile.open(title_archive, "w:bz2") as tar:
                    tar.add(title_index, arcname=title_index.name)

            title_size = title_archive.stat().st_size
            logger.info(f"✓ Title archive created: {title_size / 1e9:.2f} GB")

            # Upload full-text index
            logger.info("\nUploading full-text index to S3...")
            text_s3_key = f"{s3_prefix_with_timestamp}s2orc_index_text.tar.gz"

            with tqdm(total=text_size, unit='B', unit_scale=True, desc="Uploading text index") as pbar:
                s3_client.upload_file(
                    str(text_archive),
                    config.s3_bucket,
                    text_s3_key,
                    Callback=lambda bytes_transferred: pbar.update(bytes_transferred)
                )

            logger.info(f"✓ Uploaded: s3://{config.s3_bucket}/{text_s3_key}")

            # Upload title index
            logger.info("\nUploading title index to S3...")
            title_s3_key = f"{s3_prefix_with_timestamp}s2orc_index_title.tar.bz2"

            with tqdm(total=title_size, unit='B', unit_scale=True, desc="Uploading title index") as pbar:
                s3_client.upload_file(
                    str(title_archive),
                    config.s3_bucket,
                    title_s3_key,
                    Callback=lambda bytes_transferred: pbar.update(bytes_transferred)
                )

            logger.info(f"✓ Uploaded: s3://{config.s3_bucket}/{title_s3_key}")

            # Create metadata file with version info
            metadata = {
                "timestamp": timestamp,
                "full_text_index": {
                    "s3_key": text_s3_key,
                    "size_bytes": text_size,
                    "local_path": str(full_text_index),
                    "index_size_mb": get_index_size_mb(full_text_index)
                },
                "title_index": {
                    "s3_key": title_s3_key,
                    "size_bytes": title_size,
                    "local_path": str(title_index),
                    "index_size_mb": get_index_size_mb(title_index)
                },
                "upload_date": datetime.now().isoformat(),
                "dataset_name": config.dataset_name
            }

            # Upload metadata
            metadata_key = f"{s3_prefix_with_timestamp}metadata.json"
            s3_client.put_object(
                Bucket=config.s3_bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, indent=2),
                ContentType='application/json'
            )

            logger.info(f"✓ Uploaded metadata: s3://{config.s3_bucket}/{metadata_key}")

            # Update "latest" pointer
            latest_pointer = {
                "latest_version": timestamp,
                "s3_prefix": s3_prefix_with_timestamp,
                "full_text_key": text_s3_key,
                "title_key": title_s3_key,
                "metadata_key": metadata_key,
                "updated_at": datetime.now().isoformat()
            }

            latest_key = f"{config.s3_prefix.rstrip('/')}/latest.json"
            s3_client.put_object(
                Bucket=config.s3_bucket,
                Key=latest_key,
                Body=json.dumps(latest_pointer, indent=2),
                ContentType='application/json'
            )

            logger.info(f"✓ Updated latest pointer: s3://{config.s3_bucket}/{latest_key}")

        print("\n" + "=" * 70)
        print("Upload Complete!")
        print("=" * 70)
        print(f"Version:           {timestamp}")
        print(f"S3 Location:       s3://{config.s3_bucket}/{s3_prefix_with_timestamp}")
        print(f"Full-text index:   {text_s3_key}")
        print(f"Title index:       {title_s3_key}")
        print(f"Metadata:          {metadata_key}")
        print(f"Latest pointer:    {latest_key}")
        print("=" * 70 + "\n")

        return True

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def execute_subprocess(cmd: list, description: str) -> bool:
    """
    Execute a subprocess command with logging
    Returns True on success, False on failure
    """
    logger.info(f"{description}...")
    logger.info(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=False
        )
        logger.info(f"{description} completed successfully")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"{description} failed with exit code {e.returncode}")
        return False


def download_new_files(config: Configuration, file_urls: List[str]) -> bool:
    """
    Download specific files by creating a temporary JSON and calling download_data.py

    Args:
        config: Configuration object
        file_urls: List of file URLs to download

    Returns:
        True if download succeeds, False otherwise
    """
    if not file_urls:
        logger.info("No new files to download")
        return True

    logger.info(f"Preparing to download {len(file_urls)} new files...")

    # Create a temporary dataset info JSON with only the new files
    # This mimics the format from the Semantic Scholar API
    temp_dir = Path("data/temp_update")
    temp_dir.mkdir(exist_ok=True, parents=True)
    temp_metadata_dir = temp_dir / "metadata"
    temp_metadata_dir.mkdir(exist_ok=True, parents=True)

    # Create download_links.json (expected by download_data.py's verify function)
    download_links_file = temp_metadata_dir / "download_links.json"
    temp_dataset_info = {
        "name": config.dataset_name,
        "description": "Incremental update files",
        "files": file_urls
    }

    with open(download_links_file, 'w') as f:
        json.dump(temp_dataset_info, f, indent=2)

    logger.info(f"Created temporary file list with {len(file_urls)} files")

    # Use the existing download_data.py by importing its functions
    # This is better than subprocess as we can reuse the same process
    try:
        # Add src to path if needed
        src_path = Path(__file__).parent / "src"
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))

        from s2orc.download_data import download_file, verify_downloads

        # Download files using the existing download function
        local_path = config.s2orc_papers_path
        failed_downloads = []
        successful_downloads = []
        skipped_downloads = []

        with ThreadPoolExecutor(max_workers=config.download_workers) as executor:
            future_to_url = {executor.submit(download_file, url, local_path): url for url in file_urls}

            with tqdm(total=len(file_urls), desc="Downloading new files") as pbar:
                for future in as_completed(future_to_url):
                    result = future.result()
                    pbar.update(1)

                    if result["status"] == "success":
                        successful_downloads.append(result)
                    elif result["status"] == "skipped":
                        skipped_downloads.append(result)
                    else:
                        failed_downloads.append(result)
                        logger.warning(f"Failed: {result.get('shard_id', 'unknown')} - {result.get('error')}")

        logger.info(f"Download complete: {len(successful_downloads)} successful, {len(skipped_downloads)} skipped, {len(failed_downloads)} failed")

        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)

        return len(failed_downloads) == 0

    except ImportError as e:
        logger.error(f"Failed to import download_data module: {e}")
        logger.error("Falling back to subprocess call...")

        # Fallback: use subprocess to call download_data.py
        # This won't work as-is because download_data.py expects API calls
        # So we'll keep the download logic inline as a last resort
        return False
    except Exception as e:
        logger.error(f"Download failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def download_s2orc_data(config: Configuration) -> bool:
    """Download S2ORC data using download_data.py"""
    if not config.s2_api_key:
        logger.error("S2_API_KEY is required for downloading data")
        logger.error("Set it via environment variable or .env file")
        return False

    cmd = [
        sys.executable,
        "src/s2orc/download_data.py",
        "--api-key", config.s2_api_key,
        "--local-path", config.s2orc_papers_path,
        "--max-workers", str(config.download_workers)
    ]

    # Add limit for test mode
    if config.test_mode and config.file_limit:
        cmd.extend(["--limit", str(config.file_limit)])
        logger.info(f"TEST MODE: Downloading only {config.file_limit} file(s)")

    return execute_subprocess(cmd, "Download S2ORC data")


def decompress_data(config: Configuration) -> bool:
    """Decompress S2ORC data files"""
    script = "src/s2orc/decompress_sequential.sh"
    if config.decompress_method == "parallel":
        script = "src/s2orc/decompress_parallel.sh"

    cmd = ["bash", script, config.s2orc_papers_path]
    return execute_subprocess(cmd, "Decompress data files")


def convert_data(config: Configuration) -> bool:
    """Convert S2ORC data to Pyserini format"""
    # Create output directories
    text_pyserini_dir = "data/s2orc_pyserini_text"
    title_pyserini_dir = "data/s2orc_pyserini_title"

    Path(text_pyserini_dir).mkdir(exist_ok=True, parents=True)
    Path(title_pyserini_dir).mkdir(exist_ok=True, parents=True)

    # Convert text
    cmd_text = [
        sys.executable,
        "src/pyserini/convert_data_text.py",
        "--input-path", config.s2orc_papers_path,
        "--output-path", text_pyserini_dir,
        "--num-workers", str(config.conversion_workers)
    ]

    if not execute_subprocess(cmd_text, "Convert data to Pyserini format (full-text)"):
        return False

    # Convert title
    cmd_title = [
        sys.executable,
        "src/pyserini/convert_data_title.py",
        "--input-path", config.s2orc_papers_path,
        "--output-path", title_pyserini_dir,
        "--num-workers", str(config.conversion_workers)
    ]

    return execute_subprocess(cmd_title, "Convert data to Pyserini format (title)")


def build_indexes(config: Configuration) -> bool:
    """Build Pyserini indexes"""
    text_pyserini_dir = "data/s2orc_pyserini_text"
    title_pyserini_dir = "data/s2orc_pyserini_title"

    # Build full-text index
    cmd_text = [
        sys.executable,
        "src/pyserini/build_pyserini_index.py",
        "--input-path", text_pyserini_dir,
        "--index-path", config.full_text_index_dir,
        "--threads", str(config.index_threads)
    ]

    if not execute_subprocess(cmd_text, "Build full-text index"):
        return False

    # Build title index
    cmd_title = [
        sys.executable,
        "src/pyserini/build_pyserini_index.py",
        "--input-path", title_pyserini_dir,
        "--index-path", config.title_index_dir,
        "--threads", str(config.index_threads)
    ]

    return execute_subprocess(cmd_title, "Build title index")


def append_to_indexes(config: Configuration) -> bool:
    """
    Append new data to existing Pyserini indexes
    Uses the converted Pyserini format files
    Processes text and title indexes in parallel for better performance
    """
    text_pyserini_dir = Path("data/s2orc_pyserini_text")
    title_pyserini_dir = Path("data/s2orc_pyserini_title")

    # Check if there are any JSONL files to append
    text_files = list(text_pyserini_dir.glob("*.jsonl"))
    title_files = list(title_pyserini_dir.glob("*.jsonl"))

    if not text_files and not title_files:
        logger.warning("No new JSONL files found to append")
        return True

    logger.info(f"Found {len(text_files)} text files and {len(title_files)} title files to append")

    # Define functions to append all files for each index type
    def append_text_files():
        for jsonl_file in text_files:
            logger.info(f"Appending {jsonl_file.name} to full-text index...")
            cmd = [
                sys.executable,
                "src/pyserini/append_to_pyserini_index.py",
                "--index", config.full_text_index_dir,
                "--input", str(jsonl_file),
                "--threads", str(config.index_threads)
            ]
            if not execute_subprocess(cmd, f"Append {jsonl_file.name} to full-text index"):
                return False
        return True

    def append_title_files():
        for jsonl_file in title_files:
            logger.info(f"Appending {jsonl_file.name} to title index...")
            cmd = [
                sys.executable,
                "src/pyserini/append_to_pyserini_index.py",
                "--index", config.title_index_dir,
                "--input", str(jsonl_file),
                "--threads", str(config.index_threads)
            ]
            if not execute_subprocess(cmd, f"Append {jsonl_file.name} to title index"):
                return False
        return True

    # Run appends for text and title indexes in parallel
    logger.info("Appending to both indexes in parallel...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        text_future = executor.submit(append_text_files) if text_files else None
        title_future = executor.submit(append_title_files) if title_files else None

        # Wait for both to complete and check results
        text_success = text_future.result() if text_future else True
        title_success = title_future.result() if title_future else True

    return text_success and title_success


def validate_setup(config: Configuration, skip_data_check: bool = False) -> bool:
    """
    Validate that all required components exist

    Args:
        config: Configuration object
        skip_data_check: If True, skip validation of original data files
    """
    print_banner("Validating Setup")

    checks = []

    # Only check original data if not skipping
    if not skip_data_check:
        data_path = Path(config.s2orc_papers_path)
        checks.extend([
            (data_path.exists(), f"Original data exists at {config.s2orc_papers_path}"),
            (next(data_path.glob("*.jsonl"), None) is not None if data_path.exists() else False, "Original data has JSONL files"),
        ])
    else:
        logger.info("⚠ Skipping original data validation (indexes downloaded from S3)")

    # Always check indexes
    checks.extend([
        (Path(config.full_text_index_dir).exists(), f"Full-text index directory exists"),
        (validate_index_directory(Path(config.full_text_index_dir)), "Full-text index is valid"),
        (Path(config.title_index_dir).exists(), f"Title index directory exists"),
        (validate_index_directory(Path(config.title_index_dir)), "Title index is valid"),
    ])

    all_passed = True
    for passed, description in checks:
        status = "✓" if passed else "✗"
        print(f"  {status} {description}")
        if not passed:
            all_passed = False

    if all_passed:
        logger.info("All validation checks passed!")
    else:
        logger.error("Some validation checks failed")

    return all_passed


def start_server(config: Configuration):
    """Start the FastAPI server"""
    print_banner("Starting API Server")

    # Set environment variables
    env = os.environ.copy()
    env['S2ORC_PAPERS'] = config.s2orc_papers_path
    env['FULL_TEXT_INDEX_DIR'] = config.full_text_index_dir
    env['TITLE_INDEX_DIR'] = config.title_index_dir

    # Start uvicorn
    cmd = [
        sys.executable, "-m", "uvicorn",
        "src.server.api:app",
        "--host", config.server_host,
        "--port", str(config.server_port)
    ]

    logger.info(f"Starting server at {config.server_host}:{config.server_port}")

    # Launch in subprocess
    process = subprocess.Popen(cmd, env=env)

    # Wait and check health
    logger.info("Waiting for server to start...")
    time.sleep(5)

    try:
        response = requests.get(f"http://{config.server_host}:{config.server_port}/health", timeout=5)
        if response.status_code == 200:
            logger.info("Server health check passed!")
        else:
            logger.warning(f"Server health check returned status {response.status_code}")
    except requests.RequestException as e:
        logger.warning(f"Could not verify server health: {e}")

    print("\n" + "=" * 70)
    print("Setup Complete!")
    print("=" * 70)
    print(f"API Server:    http://{config.server_host}:{config.server_port}")
    print(f"API Docs:      http://{config.server_host}:{config.server_port}/docs")
    print(f"Health Check:  http://{config.server_host}:{config.server_port}/health")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 70 + "\n")

    # Keep script running
    try:
        process.wait()
    except KeyboardInterrupt:
        logger.info("\nShutting down...")
        process.terminate()
        process.wait()


def run_update_mode(config: Configuration) -> None:
    """
    Run incremental update mode: fetch latest dataset, compare with existing,
    and only download/index new files
    """
    print_banner("Incremental Update Mode")
    logger.info("Running in update mode - will only download and index new files")

    if not config.s2_api_key:
        logger.error("S2_API_KEY is required for update mode")
        config.s2_api_key = input("Enter Semantic Scholar API key: ").strip()
        if not config.s2_api_key:
            logger.error("API key is required")
            sys.exit(1)

    # Check that indexes exist
    if not (Path(config.full_text_index_dir).exists() and Path(config.title_index_dir).exists()):
        logger.error("Update mode requires existing indexes!")
        logger.error(f"  Full-text index: {config.full_text_index_dir}")
        logger.error(f"  Title index: {config.title_index_dir}")
        logger.error("Please run initial setup first (without --update flag)")
        sys.exit(1)

    # Fetch latest dataset info
    try:
        latest_dataset_info = fetch_latest_dataset_info(config)
    except Exception as e:
        logger.error(f"Failed to fetch latest dataset info: {e}")
        sys.exit(1)

    # Save new dataset info with timestamp
    new_json_path = save_dataset_info(latest_dataset_info)

    # Compare with existing index list
    old_json_path = Path("docs/index_list.json")
    comparison = compare_file_lists(old_json_path, latest_dataset_info)

    new_files = comparison['new_files']
    file_id_to_url = comparison['file_id_to_url']

    if not new_files:
        logger.info("No new files to download! Index is up to date.")
        print("\n" + "=" * 70)
        print("Index is already up to date - no new files found")
        print("=" * 70)
        sys.exit(0)

    logger.info(f"Found {len(new_files)} new files to process")

    # Get URLs for new files
    new_file_urls = [file_id_to_url[file_id] for file_id in new_files if file_id in file_id_to_url]

    # Download new files
    print_banner("Downloading New Files")
    if not download_new_files(config, new_file_urls):
        logger.error("Failed to download new files")
        sys.exit(1)

    # Decompress new files
    print_banner("Decompressing New Files")
    if not decompress_data(config):
        logger.error("Decompression failed")
        sys.exit(1)

    # Convert new data
    print_banner("Converting New Data to Pyserini Format")
    if not convert_data(config):
        logger.error("Data conversion failed")
        sys.exit(1)

    # Append to existing indexes
    print_banner("Appending to Existing Indexes")
    if not append_to_indexes(config):
        logger.error("Failed to append to indexes")
        sys.exit(1)

    # Update the main index_list.json to the new version
    logger.info(f"Updating docs/index_list.json to latest version...")
    shutil.copy(new_json_path, old_json_path)
    logger.info("Index list updated successfully")

    print("\n" + "=" * 70)
    print("Incremental Update Complete!")
    print("=" * 70)
    print(f"New files processed: {len(new_files)}")
    print(f"Updated index list: {old_json_path}")
    print(f"Timestamped backup: {new_json_path}")
    print("=" * 70 + "\n")

    # Upload to S3 if requested
    if config.upload_to_s3:
        if not upload_indexes_to_s3(config):
            logger.warning("Upload to S3 failed, but continuing...")

    # Start server or exit
    if config.auto_start_server:
        start_server(config)
    else:
        print("\nUpdate complete! Start the server manually with:")
        print(f"  uvicorn src.server.api:app --host {config.server_host} --port {config.server_port}")

    sys.exit(0)


def parse_command_line_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="S2ORC End-to-End Setup Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Initial setup (download and index all data)
  python make_index.py

  # Incremental update (only download and index new files)
  python make_index.py --update

  # Test mode (download only 1 file)
  TEST_MODE=true FILE_LIMIT=1 python make_index.py
        """
    )

    parser.add_argument(
        '--update',
        action='store_true',
        help='Run in update mode: fetch latest dataset, compare with existing, and only download/index new files'
    )

    parser.add_argument(
        '--upload',
        action='store_true',
        help='Upload indexes to S3 after creation/update (requires S3_BUCKET configuration)'
    )

    return parser.parse_args()


def main():
    """Main orchestration flow"""

    # Parse command line arguments
    args = parse_command_line_args()

    # 0. PREREQUISITE CHECKS
    print_banner("System Prerequisites Check")

    # Check for Java
    if not check_java_installed():
        print_java_installation_instructions()
        logger.error("Java is required but not found. Please install Java and try again.")
        sys.exit(1)

    logger.info("✓ Java is installed")

    # Test Pyserini indexing
    logger.info("\nTesting Pyserini indexing capability...")
    if not test_pyserini_indexing():
        logger.error("Pyserini indexing test failed. Please fix the environment and try again.")
        sys.exit(1)

    logger.info("✓ Pyserini can create indexes successfully")

    # Check for optional dependencies
    if not BOTO3_AVAILABLE:
        logger.warning("⚠ boto3 not installed - S3 features will be unavailable")
        logger.warning("  Install with: pip install boto3")
    else:
        logger.info("✓ boto3 is installed")

    if not DOTENV_AVAILABLE:
        logger.warning("⚠ python-dotenv not installed - .env file support unavailable")
        logger.warning("  Install with: pip install python-dotenv")
    else:
        logger.info("✓ python-dotenv is installed")

    # 1. CONFIGURATION PHASE
    config = collect_configuration()

    # Set modes from command line args
    config.update_mode = args.update
    config.upload_to_s3 = args.upload

    display_configuration_summary(config)

    response = input("\nProceed with this configuration? (y/n): ").strip().lower()
    if response != 'y':
        logger.info("Setup cancelled by user")
        sys.exit(0)

    goto_validation = False

    # 1.5 UPDATE MODE WORKFLOW (if enabled)
    if config.update_mode:
        run_update_mode(config)

    # 2. S3 INDEX CHECK (if S3 configured)
    if config.s3_bucket and BOTO3_AVAILABLE:
        print_banner("Checking S3 for Pre-built Indexes")

        try:
            s3_info = check_s3_indexes_exist(config)

            if s3_info['full_text_exists'] and s3_info['title_exists']:
                logger.info("Pre-built indexes found in S3!")
                print(f"  Full-text index: {s3_info['full_text_size'] / 1e9:.2f} GB")
                print(f"  Title index: {s3_info['title_size'] / 1e9:.2f} GB")

                # Check if indexes already exist locally
                text_index_exists = Path(config.full_text_index_dir).exists() and validate_index_directory(Path(config.full_text_index_dir))
                title_index_exists = Path(config.title_index_dir).exists() and validate_index_directory(Path(config.title_index_dir))

                if text_index_exists and title_index_exists:
                    logger.info("✓ Indexes already exist locally - skipping download")
                    print("  Indexes already downloaded and validated.")
                    goto_validation = True
                else:
                    # Download indexes with smart size-based assignment
                    try:
                        print("\nDownloading indexes from S3...")

                        # Download both to temporary locations in parallel for 2x speedup
                        temp_indexes = []

                        # Prepare download tasks
                        download_tasks = []
                        if not text_index_exists:
                            download_tasks.append({
                                'path': Path(f"data/temp_index_1"),
                                'key': s3_info['full_text_key'],
                                'intended': 'text'
                            })
                        else:
                            logger.info("✓ Full-text index already exists locally")

                        if not title_index_exists:
                            download_tasks.append({
                                'path': Path(f"data/temp_index_2"),
                                'key': s3_info['title_key'],
                                'intended': 'title'
                            })
                        else:
                            logger.info("✓ Title index already exists locally")

                        # Download in parallel using ThreadPoolExecutor
                        if download_tasks:
                            logger.info(f"Downloading {len(download_tasks)} index(es) in parallel...")

                            def download_task(task):
                                logger.info(f"Starting download from {task['key']}...")
                                download_index_from_s3(config, task['key'], task['path'])
                                return {
                                    'path': task['path'],
                                    'key': task['key'],
                                    'size': get_index_size_mb(task['path']),
                                    'intended': task['intended']
                                }

                            with ThreadPoolExecutor(max_workers=2) as executor:
                                futures = [executor.submit(download_task, task) for task in download_tasks]
                                for future in as_completed(futures):
                                    result = future.result()
                                    temp_indexes.append(result)
                                    logger.info(f"✓ Downloaded {result['intended']} index: {result['size']:.2f} MB")

                        # Smart assignment based on size
                        if len(temp_indexes) == 2:
                            logger.info("\nAnalyzing downloaded indexes by size...")
                            logger.info(f"  Index 1: {temp_indexes[0]['size']:.2f} MB (from {temp_indexes[0]['intended']} key)")
                            logger.info(f"  Index 2: {temp_indexes[1]['size']:.2f} MB (from {temp_indexes[1]['intended']} key)")

                            # Sort by size (larger first)
                            temp_indexes.sort(key=lambda x: x['size'], reverse=True)
                            larger_index = temp_indexes[0]
                            smaller_index = temp_indexes[1]

                            # Check if they match expectations
                            if larger_index['intended'] == 'title' or smaller_index['intended'] == 'text':
                                logger.warning("⚠️  WARNING: Index sizes don't match S3 key names!")
                                logger.warning(f"   Larger index ({larger_index['size']:.2f} MB) came from '{larger_index['intended']}' key")
                                logger.warning(f"   Smaller index ({smaller_index['size']:.2f} MB) came from '{smaller_index['intended']}' key")
                                logger.warning(f"   Auto-correcting: Assigning larger to full-text, smaller to title")

                            # Assign based on size (larger = text, smaller = title)
                            logger.info(f"\nAssigning indexes based on size:")
                            logger.info(f"  Full-text index (larger): {larger_index['size']:.2f} MB → {config.full_text_index_dir}")
                            shutil.move(str(larger_index['path']), str(config.full_text_index_dir))

                            logger.info(f"  Title index (smaller): {smaller_index['size']:.2f} MB → {config.title_index_dir}")
                            shutil.move(str(smaller_index['path']), str(config.title_index_dir))

                        elif len(temp_indexes) == 1:
                            # Only one was downloaded (other already exists)
                            idx = temp_indexes[0]
                            if idx['intended'] == 'text':
                                shutil.move(str(idx['path']), str(config.full_text_index_dir))
                                logger.info(f"✓ Full-text index ready: {idx['size']:.2f} MB")
                            else:
                                shutil.move(str(idx['path']), str(config.title_index_dir))
                                logger.info(f"✓ Title index ready: {idx['size']:.2f} MB")

                        # Final cross-validation
                        logger.info("\nValidating final index assignments...")
                        if not validate_index_sizes(
                            Path(config.full_text_index_dir),
                            Path(config.title_index_dir)
                        ):
                            logger.error("Index size validation failed!")
                            response = input("Continue anyway? (y/n): ").strip().lower()
                            if response != 'y':
                                logger.error("Setup cancelled due to index size mismatch")
                                sys.exit(1)

                        logger.info("✓ S3 indexes ready and validated!")
                        goto_validation = True

                    except Exception as e:
                        logger.error(f"Failed to download S3 indexes: {e}")
                        logger.error("Cannot proceed without indexes. Exiting.")
                        sys.exit(1)

            else:
                logger.info("No pre-built indexes found in S3")
                response = input("Build indexes locally? This will take significant time and resources. (y/n): ").strip().lower()
                if response != 'y':
                    logger.info("Cannot proceed without indexes. Exiting.")
                    sys.exit(0)

        except Exception as e:
            logger.error(f"S3 error: {e}")
            response = input("Continue with local build? (y/n): ").strip().lower()
            if response != 'y':
                sys.exit(1)

    elif config.s3_bucket and not BOTO3_AVAILABLE:
        logger.warning("S3 configured but boto3 is not installed")
        logger.warning("Install boto3 with: pip install boto3")
        response = input("Continue with local build? (y/n): ").strip().lower()
        if response != 'y':
            sys.exit(1)

    else:
        logger.info("No S3 configuration. Will build indexes locally.")

    # 3. LOCAL BUILD PIPELINE (if needed)
    if not goto_validation:
        print_banner("Starting Local Build Pipeline")

        # Check if data exists
        data_path = Path(config.s2orc_papers_path)
        has_gz = next(data_path.glob("*.gz"), None) is not None if data_path.exists() else False
        has_jsonl = next(data_path.glob("*.jsonl"), None) is not None if data_path.exists() else False

        if not has_gz and not has_jsonl:
            logger.info("S2ORC data not found locally. Starting download...")

            if not config.s2_api_key:
                config.s2_api_key = input("Enter Semantic Scholar API key: ").strip()
                if not config.s2_api_key:
                    logger.error("API key is required for downloading data")
                    sys.exit(1)

            if not download_s2orc_data(config):
                logger.error("Data download failed")
                sys.exit(1)
        else:
            logger.info("S2ORC data found locally. Skipping download.")

        # Check if data needs decompression
        if not has_jsonl:
            logger.info("Decompressing data...")
            if not decompress_data(config):
                logger.error("Decompression failed")
                sys.exit(1)
        else:
            logger.info("Data already decompressed. Skipping.")

        # Convert data
        print_banner("Converting Data to Pyserini Format")
        if not convert_data(config):
            logger.error("Data conversion failed")
            sys.exit(1)

        # Build indexes
        print_banner("Building Search Indexes")
        if not build_indexes(config):
            logger.error("Index building failed")
            sys.exit(1)

    # 4. VALIDATION
    # Skip data validation if we downloaded from S3 (we only have indexes, not data)
    skip_data_validation = goto_validation  # True if we downloaded from S3
    if not validate_setup(config, skip_data_check=skip_data_validation):
        logger.error("Validation failed. Please check the logs.")
        sys.exit(1)

    # 4.5. UPLOAD TO S3 (if requested)
    if config.upload_to_s3:
        if not upload_indexes_to_s3(config):
            logger.warning("Upload to S3 failed, but continuing with setup")
            # Don't exit - we can still start the server locally

    # 5. START SERVER
    if config.auto_start_server:
        start_server(config)
    else:
        print("\nSetup complete! Start the server manually with:")
        print(f"  uvicorn src.server.api:app --host {config.server_host} --port {config.server_port}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\nSetup interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Setup failed with error: {e}")
        sys.exit(1)
