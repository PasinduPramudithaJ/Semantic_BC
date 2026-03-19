import os
import subprocess
import pandas as pd
from tqdm import tqdm
from pydriller import Repository
from lxml import etree
import javalang
import re
from collections import defaultdict

# =========================
# CONFIG
# =========================
GITHUB_REPOS = [
   #"https://github.com/apache/commons-lang.git"
    #"https://github.com/OpenGamma/ElSql.git"
    "https://github.com/GIScience/oshdb.git"
]

WORK_DIR = "repos"
RESULT_FILE = "final_results1.csv"

# =========================
# CLONE REPO
# =========================
def clone_repo(repo_url):
    repo_name = repo_url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(WORK_DIR, repo_name)
    if not os.path.exists(repo_path):
        subprocess.run(["git", "clone", repo_url, repo_path])
    return repo_path

# =========================
# PARSE POM
# =========================
def parse_pom(pom_content):
    deps = {}
    try:
        root = etree.fromstring(pom_content.encode())
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        for dep in root.xpath("//m:dependency", namespaces=ns):
            g = dep.find("m:groupId", namespaces=ns)
            a = dep.find("m:artifactId", namespaces=ns)
            v = dep.find("m:version", namespaces=ns)
            if g is not None and a is not None and v is not None:
                deps[f"{g.text}:{a.text}"] = v.text
    except:
        pass
    return deps

# =========================
# DEPENDENCY CHANGE
# =========================
def detect_dependency_changes(old_pom, new_pom):
    old = parse_pom(old_pom)
    new = parse_pom(new_pom)

    changes = []
    for dep in old:
        if dep in new and old[dep] != new[dep]:
            changes.append({
                "dependency": dep,
                "old_version": old[dep],
                "new_version": new[dep]
            })
    return changes

# =========================
# METHOD CALL EXTRACTION
# =========================
def extract_method_calls(code):
    calls = set()
    try:
        tree = javalang.parse.parse(code)
        for _, node in tree.filter(javalang.tree.MethodInvocation):
            calls.add(node.member)
    except:
        pass
    return calls

# =========================
# USAGE CHANGE
# =========================
def detect_usage_change(before_code, after_code):
    before = extract_method_calls(before_code)
    after = extract_method_calls(after_code)

    removed = before - after
    added = after - before

    if removed or added:
        return True, f"Removed: {removed}, Added: {added}"
    return False, "None"

# =========================
# SYNTACTIC BREAK
# =========================
def detect_syntactic_break(before_code, after_code):
    try:
        before_tree = javalang.parse.parse(before_code)
        after_tree = javalang.parse.parse(after_code)

        before_methods = {m.name for _, m in before_tree.filter(javalang.tree.MethodDeclaration)}
        after_methods = {m.name for _, m in after_tree.filter(javalang.tree.MethodDeclaration)}

        removed = before_methods - after_methods

        if removed:
            return True, f"Removed methods: {removed}"
        return False, "None"
    except:
        return False, "Parse Error"

# =========================
# SEMANTIC CHANGE
# =========================
def detect_semantic_change_ast(before_code, after_code):
    try:
        before_tree = javalang.parse.parse(before_code)
        after_tree = javalang.parse.parse(after_code)

        changes = []

        if str(before_tree) != str(after_tree):
            changes.append("Logic changed")

        return " | ".join(changes) if changes else "None"

    except:
        if before_code != after_code:
            return "Fallback textual change"
        return "None"

# =========================
# ADVANCED CLASSIFIER
# =========================
def classify_response(commit_msg):
    msg = commit_msg.lower()

    categories = {
        "Bug Fix": ["fix", "bug", "error", "issue", "patch"],
        "Dependency Update": ["update", "upgrade", "bump", "dependency"],
        "Refactoring": ["refactor", "cleanup", "restructure"],
        "Migration": ["migrate", "migration"],
        "Exception Handling": ["exception", "try-catch"],
        "Logging": ["log", "logging"],
        "Performance Improvement": ["optimiz", "performance"],
        "Feature Addition": ["feature", "add", "implement"],
        "Testing": ["test"],
        "Code Formatting": ["format", "style", "lint"],
        "Documentation": ["doc", "readme"],
        "Build/CI": ["build", "ci", "pipeline"]
    }

    matched = []

    for cat, keys in categories.items():
        for k in keys:
            if re.search(r"\b" + re.escape(k), msg):
                matched.append(cat)
                break

    priority = [
        "Bug Fix",
        "Dependency Update",
        "Migration",
        "Performance Improvement",
        "Refactoring",
        "Feature Addition",
        "Testing",
        "Documentation",
        "Build/CI",
        "Logging",
        "Exception Handling",
        "Code Formatting"
    ]

    for p in priority:
        if p in matched:
            return p

    return "Other"

# =========================
# ANALYSIS
# =========================
def analyze_repo(repo_path, max_commits):
    results = []
    stats = defaultdict(int)

    commits = list(Repository(repo_path).traverse_commits())

    if max_commits != "all":
        commits = commits[:min(int(max_commits), len(commits))]

    for commit in tqdm(commits, desc=f"Analyzing {repo_path}"):

        dep_changes = []

        for f in commit.modified_files:
            if f.filename == "pom.xml" and f.source_code_before and f.source_code:
                dep_changes = detect_dependency_changes(f.source_code_before, f.source_code)
                break

        if not dep_changes:
            continue

        syntactic_flag = False
        semantic_flag = False

        for f in commit.modified_files:
            if f.filename.endswith(".java") and f.source_code_before and f.source_code:

                usage_flag, usage_details = detect_usage_change(f.source_code_before, f.source_code)
                syn_flag, syn_details = detect_syntactic_break(f.source_code_before, f.source_code)

                sem_details = detect_semantic_change_ast(f.source_code_before, f.source_code)
                sem_flag = sem_details != "None"

                syntactic_flag |= syn_flag
                semantic_flag |= sem_flag

                for dep in dep_changes:
                    results.append({
                        "repo": repo_path,
                        "commit": commit.hash,
                        "date": commit.committer_date,
                        "dependency": dep["dependency"],
                        "old_version": dep["old_version"],
                        "new_version": dep["new_version"],
                        "file": f.filename,

                        "usage_changed": usage_flag,
                        "usage_details": usage_details,

                        "syntactic_break": syn_flag,
                        "syntactic_details": syn_details,

                        "semantic_change": sem_flag,
                        "semantic_details": sem_details,

                        "commit_message": commit.msg.split('\n')[0],
                        "response_type": classify_response(commit.msg)
                    })

        # ===== STATS =====
        if syntactic_flag and semantic_flag:
            stats["both"] += 1
        elif syntactic_flag:
            stats["syntactic_only"] += 1
        elif semantic_flag:
            stats["semantic_only"] += 1
        else:
            stats["none"] += 1

    return results, stats

# =========================
# MAIN
# =========================
def main():
    os.makedirs(WORK_DIR, exist_ok=True)

    all_results = []
    total_stats = defaultdict(int)

    user_input = input("Enter number of commits (or 'all'): ").strip()
    max_commits = user_input if user_input == "all" else int(user_input)

    for repo in GITHUB_REPOS:
        print(f"\nProcessing {repo}")
        path = clone_repo(repo)

        res, stats = analyze_repo(path, max_commits)
        all_results.extend(res)

        for k in stats:
            total_stats[k] += stats[k]

    # SAVE CSV
    df = pd.DataFrame(all_results)
    df.to_csv(RESULT_FILE, index=False)

    # ===== FINAL STATS =====
    total = sum(total_stats.values())

    print("\n===== FINAL STATISTICS =====")
    for k, v in total_stats.items():
        perc = (v / total * 100) if total else 0
        print(f"{k}: {v} ({perc:.2f}%)")

    print("\nSaved to:", RESULT_FILE)


if __name__ == "__main__":
    main()