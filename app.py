import json
import os, sys
import hashlib
import time
import io
import base64
import requests
import zipfile
from datetime import datetime
from azure.storage.blob import BlobServiceClient
from tqdm import tqdm
import argparse


if os.path.exists('secret.json'):
    with open('secret.json', 'r') as f:
        secrets = json.load(f)
else:

    parser = argparse.ArgumentParser()
    parser.add_argument('--account_url', required=True)
    parser.add_argument('--container_name', required=True)
    parser.add_argument('--account_key', required=True)
    parser.add_argument('--jamf_url', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()

    secrets = {
        'account_url': args.account_url,
        'container_name': args.container_name,
        'account_key': args.account_key,
        'jamf_url': args.jamf_url,
        'username': args.username,
        'password': args.password
    }

required_keys = ['account_url', 'container_name', 'account_key', 'jamf_url', 'username', 'password']
for key in required_keys:
    if not secrets.get(key):
        print(f"Error: Missing or empty required environment variable '{key}'.")
        sys.exit(1)

account_url = secrets['account_url']
container_name = secrets['container_name']
account_key = secrets['account_key']
jamf_url = secrets['jamf_url']
username = secrets['username']
password = secrets['password']

log_folder = "logs"
temp_folder = "temp"
log_entries = []

blob_service_client = BlobServiceClient(account_url=account_url, credential=account_key)
container_client = blob_service_client.get_container_client(container_name)

os.makedirs(temp_folder, exist_ok=True)
for file in os.listdir(temp_folder):
    os.remove(os.path.join(temp_folder, file))

def ensure_blob_folder_exists(folder_name):
    blobs = container_client.list_blobs(name_starts_with=folder_name)
    if not any(blobs):
        blob_client = container_client.get_blob_client(blob=folder_name + '/blob')
        blob_client.upload_blob(b"", overwrite=True)

def get_jamf_api_token():
    url = f"{jamf_url}/api/v1/auth/token"
    response = requests.post(url, auth=(username, password))
    if response.status_code == 200:
        token_response = response.json()
        return token_response['token'], token_response['expires']
    else:
        print(f"Error fetching API token: {response.status_code}, {response.text}")
        sys.exit(1)

def calculate_md5(file_path):
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(4096):
            md5.update(chunk)
    return md5.hexdigest()

def upload_log_to_blob():
    log_filename = f"{log_folder}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    log_content = "\n".join(log_entries)
    log_data = io.BytesIO(log_content.encode('utf-8'))
    blob_client = container_client.get_blob_client(blob=log_filename)
    blob_client.upload_blob(log_data, overwrite=True)
    print(f"Log file '{log_filename}' uploaded successfully.")

def upload_file_in_chunks(file_path, blob_client):
    block_list = []
    chunk_size = 4 * 1024 * 1024
    file_size = os.path.getsize(file_path)

    try:
        with open(file_path, "rb") as file, tqdm(total=file_size, unit='B', unit_scale=True, desc=os.path.basename(file_path)) as progress:
            index = 0
            while True:
                chunk_data = file.read(chunk_size)
                if not chunk_data:
                    break
                block_id = base64.b64encode(f"block-{index}".encode()).decode('utf-8')
                try:
                    blob_client.stage_block(block_id=block_id, data=chunk_data)
                    block_list.append(block_id)
                    progress.update(len(chunk_data))
                except Exception as e:
                    print(f"Error uploading chunk {index + 1} of {os.path.basename(file_path)}: {e}")
                    return False
                index += 1

        blob_client.commit_block_list(block_list)
        print(f"File '{file_path}' fully uploaded to Azure Blob storage.")
        return True
    except Exception as e:
        print(f"Failed to upload '{file_path}' in chunks: {e}")
        return False

def zip_file(file_path):
    zip_path = f"{file_path}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    print(f"File '{file_path}' compressed to '{zip_path}'.")
    return zip_path

def process_package(package, headers, existing_blobs):
    package_name = package["name"]
    package_id = package["id"]
    file_path = os.path.join(temp_folder, package_name)

    if package_name not in existing_blobs and f"{package_name}.zip" not in existing_blobs:
        print(f"Downloading {package_name}...")
        download_url = f"{jamf_url}/api/v1/jcds/files/{package_name}"
        url_response = requests.get(download_url, headers=headers)
        if url_response.status_code == 200:
            download_uri = url_response.json().get("uri")
            download_response = requests.get(download_uri, stream=True)
            if download_response.status_code == 200:
                with open(file_path, "wb") as f:
                    for chunk in download_response.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)

                md5_hash = calculate_md5(file_path)
                blob_client = container_client.get_blob_client(blob=package_name)

                if not upload_file_in_chunks(file_path, blob_client):
                    print(f"Initial upload failed for {package_name}. Attempting to zip and retry...")
                    zip_path = zip_file(file_path)
                    zip_blob_client = container_client.get_blob_client(blob=f"{package_name}.zip")
                    if upload_file_in_chunks(zip_path, zip_blob_client):
                        log_entries.append(f"Uploaded: {package_name}.zip")
                        print(f"Uploaded '{package_name}.zip' to Azure Blob storage.")
                        os.remove(zip_path)
                    else:
                        log_entries.append(f"Failed to upload: {package_name}.zip")
                        print(f"Failed to upload '{package_name}.zip'")
                else:
                    log_entries.append(f"Uploaded: {package_name}")
                    print(f"Uploaded {package_name} to Azure Blob storage.")

                os.remove(file_path)
            else:
                log_entries.append(f"Failed to download: {package_name}")
        else:
            log_entries.append(f"Failed to retrieve URL for: {package_name}")
    else:
        print(f"Already exists: {package_name} or {package_name}.zip")
        log_entries.append(f"Skipped (already exists): {package_name} or {package_name}.zip")

def upload_packages_to_blob():
    ensure_blob_folder_exists(log_folder)

    token, expires = get_jamf_api_token()
    if token is None:
        print("Unable to retrieve API token.")
        sys.exit(1)
    headers = {'Authorization': f"Bearer {token}", 'Accept': "application/json"}

    existing_blobs = [blob.name for blob in container_client.list_blobs()]

    packages_url = f"{jamf_url}/JSSResource/packages"
    response = requests.get(packages_url, headers=headers)
    if response.status_code != 200:
        print("Error retrieving package list:", response.status_code)
        return

    package_list = response.json().get("packages", [])

    for package in package_list:
        process_package(package, headers, existing_blobs)

    upload_log_to_blob()

upload_packages_to_blob()
