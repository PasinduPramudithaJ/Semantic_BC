import os
import subprocess
import pandas as pd
from tqdm import tqdm
from pydriller import Repository
from lxml import etree
import javalang

# =========================
# CONFIG
# =========================
GITHUB_REPOS = [
    #"https://github.com/apache/commons-lang.git"
    "https://github.com/OpenGamma/ElSql.git"
    #"https://github.com/GIScience/oshdb.git"
]

WORK_DIR = "repos"
RESULT_FILE = "results_dep_semantic_1.csv"

# =========================
# UTIL: CLONE REPO
# =========================
def clone_repo(repo_url):
    repo_name = repo_url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(WORK_DIR, repo_name)
    if not os.path.exists(repo_path):
        subprocess.run(["git", "clone", repo_url, repo_path])
    return repo_path

# =========================
# PARSE pom.xml
# =========================
def parse_pom(pom_content):
    dependencies = {}
    try:
        root = etree.fromstring(pom_content.encode())
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        for dep in root.xpath("//m:dependency", namespaces=ns):
            group = dep.find("m:groupId", namespaces=ns)
            artifact = dep.find("m:artifactId", namespaces=ns)
            version = dep.find("m:version", namespaces=ns)
            if group is not None and artifact is not None and version is not None:
                key = f"{group.text}:{artifact.text}"
                dependencies[key] = version.text
    except Exception:
        pass
    return dependencies

# =========================
# DETECT VERSION CHANGES
# =========================
def detect_dependency_changes(old_pom, new_pom):
    old_deps = parse_pom(old_pom)
    new_deps = parse_pom(new_pom)
    changes = []
    for dep in old_deps:
        if dep in new_deps and old_deps[dep] != new_deps[dep]:
            changes.append({
                "dependency": dep,
                "old_version": old_deps[dep],
                "new_version": new_deps[dep]
            })
    return changes

# =========================
# AST-BASED SEMANTIC CHANGE DETECTION
# =========================
def detect_semantic_change_ast(before_code, after_code):
    """
    Detects detailed semantic changes in Java code by explicitly comparing 
    AST structures (Imports, Methods, Fields, and Internal Node sequences).
    Returns a descriptive string of the changes.
    """
    try:
        before_tree = javalang.parse.parse(before_code)
        after_tree = javalang.parse.parse(after_code)

        changes = []

        # 1. Compare Imports
        before_imports = {imp.path for imp in before_tree.imports}
        after_imports = {imp.path for imp in after_tree.imports}
        added_imports = after_imports - before_imports
        removed_imports = before_imports - after_imports
        if added_imports: changes.append(f"Added imports: {len(added_imports)}")
        if removed_imports: changes.append(f"Removed imports: {len(removed_imports)}")

        # 2. Helper to get method/field signatures
        def get_declarations(tree):
            methods = set()
            fields = set()
            
            for path, node in tree.filter(javalang.tree.MethodDeclaration):
                try:
                    params = ",".join([p.type.name for p in node.parameters if hasattr(p, 'type') and hasattr(p.type, 'name')])
                    methods.add(f"{node.name}({params})")
                except Exception:
                    methods.add(node.name)
                    
            for path, node in tree.filter(javalang.tree.FieldDeclaration):
                for dec in node.declarators:
                    fields.add(dec.name)
                    
            return methods, fields

        b_methods, b_fields = get_declarations(before_tree)
        a_methods, a_fields = get_declarations(after_tree)

        # 3. Compare Methods
        added_methods = a_methods - b_methods
        removed_methods = b_methods - a_methods
        if added_methods: changes.append(f"Added methods: {', '.join(added_methods)}")
        if removed_methods: changes.append(f"Removed methods: {', '.join(removed_methods)}")

        # 4. Compare Fields
        added_fields = a_fields - b_fields
        removed_fields = b_fields - a_fields
        if added_fields: changes.append(f"Added fields: {', '.join(added_fields)}")
        if removed_fields: changes.append(f"Removed fields: {', '.join(removed_fields)}")

        # 5. Check for Method Body / Internal Logic changes
        # If no structural elements were added/removed, but the code still differs
        if not changes:
            # Flatten the tree into an ordered list of node types and specific values
            b_nodes = [(type(n).__name__, getattr(n, 'name', None), getattr(n, 'value', None)) for path, n in before_tree]
            a_nodes = [(type(n).__name__, getattr(n, 'name', None), getattr(n, 'value', None)) for path, n in after_tree]
            
            if b_nodes != a_nodes:
                changes.append("Internal logic / Method body changed")

        return " | ".join(changes) if changes else "None"

    except javalang.parser.JavaSyntaxError:
        # Fallback if AST parsing fails due to incomplete/unparseable snippets
        if before_code.strip() != after_code.strip():
            return "Syntax Error (Fallback: Textual logic changed)"
        return "None"
    except Exception:
        if before_code.strip() != after_code.strip():
            return "Parsing Exception (Fallback: Textual logic changed)"
        return "None"

# =========================
# CLASSIFY DEVELOPER RESPONSE
# =========================
def classify_response(commit_msg):
    msg = commit_msg.lower()
    if "fix" in msg or "bug" in msg or "error" in msg:
        return "Bug Fix"
    elif "update" in msg or "upgrade" in msg or "bump" in msg:
        return "Dependency Update"
    elif "refactor" in msg or "cleanup" in msg:
        return "Refactoring"
    elif "migrate" in msg:
        return "Migration"
    elif "exception" in msg or "try-catch" in msg:
        return "Exception Handling"
    elif "log" in msg or "logging" in msg:
        return "Logging"
    elif "optimiz" in msg or "performance" in msg:
        return "Performance Improvement"
    elif "feature" in msg or "add" in msg or "implement" in msg:
        return "Feature Addition"
    elif "test" in msg:
        return "Testing"
    elif "format" in msg or "style" in msg or "lint" in msg:
        return "Code Formatting"
    else:
        return "Other"

# =========================
# MAIN ANALYSIS
# =========================
def analyze_repo(repo_path, max_commits):
    results = []
    commits = list(Repository(repo_path).traverse_commits())
    
    # Ensure max_commits filter is applied safely
    if max_commits != "all":
        # Check to avoid index out of bounds if repo has fewer commits than requested
        limit = min(int(max_commits), len(commits))
        commits = commits[:limit]

    for commit in tqdm(commits, desc=f"Analyzing {repo_path}"):
        modified_files = commit.modified_files

        # Check for dependency updates in this commit
        dep_changes = []
        for file in modified_files:
            if file.filename == "pom.xml" and file.source_code_before and file.source_code:
                dep_changes = detect_dependency_changes(file.source_code_before, file.source_code)
                break

        if dep_changes:
            # Check **each Java file individually**
            for f in modified_files:
                if f.filename.endswith(".java") and f.source_code_before and f.source_code:
                    semantic_details = detect_semantic_change_ast(f.source_code_before, f.source_code)
                    semantic_flag = (semantic_details != "None")

                    # Record all dependency updates for this Java file
                    for change in dep_changes:
                        results.append({
                            "repo": repo_path,
                            "commit": commit.hash,
                            "date": commit.committer_date,
                            "dependency": change["dependency"],
                            "old_version": change["old_version"],
                            "new_version": change["new_version"],
                            "java_file": f.filename,
                            "semantic_change_detected": semantic_flag,
                            "semantic_change_details": semantic_details, # New Detailed Output Column
                            "commit_message": commit.msg.split('\n')[0], # Truncated to first line for cleaner CSVs
                            "response_type": classify_response(commit.msg)
                        })

    return results

# =========================
# RUN PIPELINE
# =========================
def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    all_results = []

    # Ask user for number of commits
    user_input = input("Enter number of commits to analyze (type 'all' for all commits): ").strip()
    max_commits = user_input if user_input.lower() == "all" else int(user_input)

    for repo in GITHUB_REPOS:
        print(f"\nProcessing {repo}")
        path = clone_repo(repo)
        repo_results = analyze_repo(path, max_commits)
        all_results.extend(repo_results)

    if all_results:
        df = pd.DataFrame(all_results)
        df.to_csv(RESULT_FILE, index=False)
        print("\nAnalysis Complete!")
        print(df.head())
    else:
        print("\nAnalysis Complete! No dependency updates with Java file changes were found in the specified commits.")

if __name__ == "__main__":
    main()