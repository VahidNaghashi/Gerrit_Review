import os
import requests
import base64
import json
from urllib.parse import quote
import re

GERRIT_USER = os.getenv("GERRIT_USER")
GERRIT_PASS = os.getenv("GERRIT_PASS")
GERRIT_URL = os.getenv("GERRIT_URL")
LLM_API = os.getenv("LLM_API", "http://localhost:8006/rate_code")

BOT_TAG = "llm-review-bot"

def get_auth_header():
    credentials = f"{GERRIT_USER}:{GERRIT_PASS}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}

def get_open_changes():
    url = f"{GERRIT_URL}/a/changes/?q=status:open&o=CURRENT_REVISION"

    response = requests.get(url, headers=get_auth_header(), verify=False)
    json_data = response.text.lstrip(")]}'\n")
    return json.loads(json_data)

def get_change_files(change_id, revision_id):
    url = f"{GERRIT_URL}/a/changes/{change_id}/revisions/{revision_id}/files/"
    response = requests.get(url, headers=get_auth_header(), verify=False)
    json_data = response.text.lstrip(")]}'\n")
    return json.loads(json_data)


def fallback_full_file_lines(change_id, revision_id, filepath):
    url = f"{GERRIT_URL}/a/changes/{change_id}/revisions/{revision_id}/files/{quote(filepath, safe='')}/content"
    response = requests.get(url, headers=get_auth_header(), verify=False)

    if response.status_code != 200:
        print(f" Failed to fetch full file for {filepath}")
        return []

    try:
        content = base64.b64decode(response.text).decode(errors="ignore")
        return [(i + 1, line) for i, line in enumerate(content.splitlines()) if line.strip()]
    except Exception as e:
        print(f" Failed to decode full file for {filepath}: {e}")
        return []



def get_patch_added_lines(change_id, revision_id, filepath):
    url = f"{GERRIT_URL}/a/changes/{change_id}/revisions/{revision_id}/files/{quote(filepath, safe='')}/patch"
    response = requests.get(url, headers=get_auth_header(), verify=False)

    if response.status_code != 200:
        print(f" Failed to fetch patch for {filepath} (status {response.status_code})")
        return fallback_full_file_lines(change_id, revision_id, filepath)

    try:
        patch = base64.b64decode(response.text).decode(errors="ignore")
    except Exception as e:
        print(f" Failed to decode patch: {e}")
        return fallback_full_file_lines(change_id, revision_id, filepath)

    added_lines = []
    current_line_num = None

    for line in patch.splitlines():
        if line.startswith('@@'):
            match = re.search(r"\+(\d+)", line)
            if match:
                current_line_num = int(match.group(1)) - 1
        elif line.startswith('+') and not line.startswith('+++'):
            current_line_num += 1
            content = line[1:].strip()
            if content:
                added_lines.append((current_line_num, content))
        elif line.startswith('-') or line.startswith(' '):
            if current_line_num is not None:
                current_line_num += 1

    if not added_lines:
        print(f" No added lines in patch. Falling back to full file: {filepath}")
        return fallback_full_file_lines(change_id, revision_id, filepath)

    return added_lines



def generate_llm_comment_for_line(code_line):
    try:
        response = requests.post(
            LLM_API,
            json={"code": code_line},
            timeout=30,
            verify=False
        )
        response.raise_for_status()
        content = response.json()["comment"].strip()
        return content if content else None
    except Exception as e:
        print(f" LLM failed for line: {e}")
        return None


def post_inline_comments(change_id, revision_id, file_to_comments):
    body = {
        "message": " LLM inline review",
        "tag": "llm-review-bot",
        "comments": file_to_comments
    }

    url = f"{GERRIT_URL}/a/changes/{change_id}/revisions/{revision_id}/review"
    response = requests.post(url, headers={**get_auth_header(), "Content-Type": "application/json"}, json=body, verify=False)
    if response.status_code in (200, 201):
        print(f" Inline review posted for change {change_id}")
    else:
        print(f" Failed to post inline comments: {response.status_code}")
        print(response.text)


def review_change_inline(change_id, revision_id, file_path):
    print(f" Reviewing {file_path} line-by-line...")

    added_lines = get_patch_added_lines(change_id, revision_id, file_path)
    if not added_lines:
        print(" No added lines found.")
        return

    comments = []
    for line_num, code_line in added_lines:
        comment = generate_llm_comment_for_line(code_line)
        if comment:
            comments.append({
                "line": line_num,
                "message": comment
            })

    if comments:
        post_inline_comments(change_id, revision_id, {file_path: comments})
    else:
        print(" No comments generated by LLM.")


def get_change_detail(change_number):
    url = f"{GERRIT_URL}/a/changes/{change_number}/?o=CURRENT_REVISION"
    response = requests.get(url, headers=get_auth_header(), verify=False)
    if response.status_code != 200:
        print(f" Failed to fetch change details: {response.status_code}")
        return None
    json_data = response.text.lstrip(")]}'\n")
    return json.loads(json_data)


change = get_change_detail("74787")
change_id = change["id"]
revision_id = change["current_revision"]

for file_path in get_change_files(change_id, revision_id):
    if file_path == "/COMMIT_MSG":
        continue
    review_change_inline(change_id, revision_id, file_path)
