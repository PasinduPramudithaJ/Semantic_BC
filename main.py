import os
import subprocess
from pydriller import Repository
from pathlib import Path
import shutil

# =========================
# CONFIG
# =========================
GITHUB_REPOS = [
    #"https://github.com/GIScience/oshdb.git",
    "https://github.com/apache/commons-lang.git",
    #"https://github.com/apache/commons-logging.git"
]

WORK_DIR = "repos"
OUTPUT_DIR = "jars"
NUM_COMMITS = 10  # Process the last N commits

# Maven folder inside project - Ensure this folder exists in your script directory
MVN_DIR = "maven-mvnd-1.0.5-windows-amd64" 
MVND_CMD_PATH = Path(MVN_DIR) / "bin" / "mvnd.cmd"

# =========================
# UTILS
# =========================
def verify_mvnd():
    """Ensure mvnd exists and return its ABSOLUTE path"""
    if not MVND_CMD_PATH.exists():
        raise FileNotFoundError(f"[!] mvnd.cmd not found at {MVND_CMD_PATH.resolve()}")
    
    # CRITICAL: Convert to absolute path so it works when cwd changes to repo folders
    absolute_mvnd = str(MVND_CMD_PATH.resolve())
    print(f"[+] Using mvnd (Absolute): {absolute_mvnd}")
    return absolute_mvnd

# =========================
# GIT OPERATIONS
# =========================
def clone_repo(repo_url):
    repo_name = repo_url.split("/")[-1].replace(".git", "")
    repo_path = os.path.abspath(os.path.join(WORK_DIR, repo_name))
    if not os.path.exists(repo_path):
        print(f"[*] Cloning {repo_url}...")
        subprocess.run(["git", "clone", repo_url, repo_path], check=True)
    return repo_path

def get_last_commits(repo_path, n):
    print(f"[*] Analyzing commit history for {os.path.basename(repo_path)}...")
    # Pydriller returns oldest -> newest
    commits = list(Repository(repo_path).traverse_commits())
    recent_commits = commits[-n:] if len(commits) >= n else commits
    return recent_commits

def checkout_commit(repo_path, commit_hash):
    """Clean the working directory and checkout a specific hash"""
    # Force clean to prevent 'untracked files' errors during checkout
    subprocess.run(["git", "reset", "--hard"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fdx"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", commit_hash], cwd=repo_path, check=True, capture_output=True)

# =========================
# BUILD ENGINE
# =========================
def build_jar(repo_path, jar_name, mvnd_cmd_abs):
    print(f"[*] Building {jar_name}...")
    
    # Run maven package using the absolute path to mvnd
    result = subprocess.run(
        [mvnd_cmd_abs, "clean", "package", "-DskipTests"], 
        cwd=repo_path, 
        shell=True # Recommended for .cmd files on Windows
    )
    
    if result.returncode != 0:
        print(f"[!] Build failed for {jar_name}")
        return None

    repo_path_obj = Path(repo_path)
    
    # Find all jars, excluding common non-runnable artifacts
    jars = [j for j in repo_path_obj.rglob("*.jar") 
            if "target" in str(j) 
            and not any(x in j.name for x in ["-sources", "-javadoc", "-tests", "original-"])]

    if not jars:
        print(f"[!] No valid application jar found for {jar_name}")
        return None

    # Pick the largest jar (usually the shaded/fat jar in multi-module projects)
    target_jar = sorted(jars, key=lambda x: x.stat().st_size, reverse=True)[0]
    
    dest = Path(OUTPUT_DIR) / f"{jar_name}.jar"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(target_jar), str(dest))
    
    print(f"[+] Success! Jar saved to: {dest}")
    return dest

# =========================
# MAIN EXECUTION
# =========================
def main():
    # Setup directories
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        mvnd_abs_path = verify_mvnd()
    except FileNotFoundError as e:
        print(e)
        return

    for repo_url in GITHUB_REPOS:
        repo_path = clone_repo(repo_url)
        commits = get_last_commits(repo_path, NUM_COMMITS)

        for i, commit in enumerate(commits, start=1):
            print(f"\n>>> Processing Commit {i}/{len(commits)} [{commit.hash[:8]}]")
            
            checkout_commit(repo_path, commit.hash)
            
            # Label based on sequence for your semantic analysis
            jar_label = f"semantic-app-commit-{i}"
            build_jar(repo_path, jar_label, mvnd_abs_path)

        # Cleanup: Return to the main/master branch
        print("\n[*] Returning repository to default branch...")
        subprocess.run(["git", "checkout", "-"], cwd=repo_path, capture_output=True)

if __name__ == "__main__":
    main()