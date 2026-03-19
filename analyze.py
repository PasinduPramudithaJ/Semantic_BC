import pandas as pd
import matplotlib.pyplot as plt
import os
from collections import Counter

# =========================
# CONFIG
# =========================
INPUT_FILE = "results_dep_semantic_2.csv"  # Updated to match the output of the previous script
OUTPUT_SUMMARY = "final_summary_full.csv"
OUTPUT_DIR = "analysis_outputs"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================
# LOAD DATA
# =========================
print("[INFO] Loading dataset...")
try:
    df = pd.read_csv(INPUT_FILE)
    print(f"[INFO] Initial Data Shape: {df.shape}")
except FileNotFoundError:
    print(f"[ERROR] Could not find {INPUT_FILE}. Please run the detection script first.")
    exit(1)

# =========================
# CLEAN DATA
# =========================
print("[INFO] Cleaning data...")
df = df.drop_duplicates()
df = df.dropna(subset=["commit"])  # Ensure commit field exists

# The target column is now a boolean: True/False
# Ensure it's treated as boolean
df["semantic_change_detected"] = df["semantic_change_detected"].astype(bool)

print(f"[INFO] Cleaned Data Shape: {df.shape}")

# =========================
# BASIC METRICS
# =========================
total_file_updates = len(df)
unique_commits = df["commit"].nunique()

semantic_count = df["semantic_change_detected"].sum()
non_semantic_count = total_file_updates - semantic_count

semantic_percentage = (semantic_count / total_file_updates) * 100 if total_file_updates else 0

print("\n========== BASIC METRICS ==========")
print(f"Total Unique Commits Analyzed: {unique_commits}")
print(f"Total Java Files Updated Alongside Dependencies: {total_file_updates}")
print(f"Files with Semantic Changes: {semantic_count}")
print(f"Files with NO Semantic Changes: {non_semantic_count}")
print(f"Semantic Change Rate per File: {semantic_percentage:.2f}%")
print("===================================\n")

# =========================
# RESPONSE ANALYSIS
# =========================
print("[INFO] Analyzing developer responses...")
# Since we have multiple rows per commit (one for each file), 
# we should drop duplicates by commit to get an accurate count of *commit intentions*
commits_df = df.drop_duplicates(subset=["commit", "response_type"])
response_counts = commits_df["response_type"].value_counts()
response_percentages = (response_counts / len(commits_df)) * 100

print("\n========== RESPONSE DISTRIBUTION (Per Commit) ==========")
print(response_counts)
print("\nPercentages:")
print(response_percentages.apply(lambda x: f"{x:.2f}%"))
print("========================================================\n")

# =========================
# CATEGORY CREATION
# =========================
df["bc_category"] = df["semantic_change_detected"].apply(
    lambda x: "Semantic Change" if x else "No Semantic Change"
)
category_counts = df["bc_category"].value_counts()

# =========================
# CROSS ANALYSIS
# =========================
print("[INFO] Cross analyzing Changes vs Response Types...")
cross_tab = pd.crosstab(df["response_type"], df["bc_category"])
print("\n========== CROSS TAB ==========")
print(cross_tab)
print("================================\n")

# =========================
# DETAILED SEMANTIC INSIGHTS
# =========================
print("[INFO] Extracting detailed semantic change types...")
semantic_details = df[df["semantic_change_detected"]]["semantic_change_details"].dropna()

# Split the " | " separated strings and count occurrences
all_changes = []
for detail in semantic_details:
    # Remove the count numbers (e.g., "Added imports: 2" -> "Added imports") for cleaner aggregation
    parts = [p.split(":")[0].strip() for p in detail.split(" | ")]
    all_changes.extend(parts)

change_counter = Counter(all_changes)
change_df = pd.DataFrame.from_dict(change_counter, orient='index', columns=['Count']).sort_values(by='Count', ascending=False)

print("\n========== SEMANTIC CHANGE TYPES ==========")
print(change_df)
print("===========================================\n")

# =========================
# FILE-LEVEL INSIGHTS
# =========================
# Calculate average number of semantically changed files per commit
semantic_commits = df[df["semantic_change_detected"]].groupby("commit").size()
avg_files_per_semantic_change = semantic_commits.mean() if not semantic_commits.empty else 0

print("\n========== FILE-LEVEL INSIGHTS ==========")
print(f"Avg files with semantic changes per commit (when a semantic change occurs): {avg_files_per_semantic_change:.2f}")
print("========================================\n")

# =========================
# SAVE SUMMARY
# =========================
summary_df = pd.DataFrame({
    "Metric": [
        "Total Unique Commits",
        "Total File Updates",
        "Semantic Changes",
        "Non-Semantic Changes",
        "Semantic Change Rate (%)",
        "Avg Files per Semantic Commit"
    ],
    "Value": [
        unique_commits,
        total_file_updates,
        semantic_count,
        non_semantic_count,
        round(semantic_percentage, 2),
        round(avg_files_per_semantic_change, 2)
    ]
})
summary_path = os.path.join(OUTPUT_DIR, OUTPUT_SUMMARY)
summary_df.to_csv(summary_path, index=False)
print(f"[INFO] Summary saved to {summary_path}")

# =========================
# VISUALIZATIONS
# =========================
print("[INFO] Generating visualizations...")

# 1. Response distribution
fig, ax = plt.subplots(figsize=(10, 6))
response_counts.plot(kind="bar", ax=ax, color='skyblue', edgecolor='black')
ax.set_title("Developer Responses to Dependency Updates")
ax.set_xlabel("Response Type")
ax.set_ylabel("Number of Commits")
plt.xticks(rotation=45, ha='right')
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "response_distribution.png"))
plt.close()

# 2. Semantic vs Non-Semantic
fig, ax = plt.subplots(figsize=(8, 6))
category_counts.plot(kind="pie", autopct='%1.1f%%', ax=ax, colors=['lightgreen', 'lightcoral'])
ax.set_title("File Updates: Semantic vs Non-Semantic Changes")
ax.set_ylabel("") # Hide y-label for pie charts
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "bc_distribution.png"))
plt.close()

# 3. Cross analysis (Response vs BC)
fig, ax = plt.subplots(figsize=(12, 6))
cross_tab.plot(kind="bar", stacked=True, ax=ax, colormap='Set2', edgecolor='black')
ax.set_title("Semantic Changes by Commit Response Type")
ax.set_xlabel("Response Type")
ax.set_ylabel("Number of File Updates")
plt.xticks(rotation=45, ha='right')
plt.legend(title="Change Category")
plt.tight_layout()
plt.savefig(os.path.join(OUTPUT_DIR, "cross_analysis.png"))
plt.close()

# 4. Detailed Semantic Change Types
if not change_df.empty:
    fig, ax = plt.subplots(figsize=(10, 6))
    change_df['Count'].plot(kind="barh", ax=ax, color='orange', edgecolor='black')
    ax.set_title("Types of Semantic Changes Detected")
    ax.set_xlabel("Frequency")
    ax.set_ylabel("Change Type")
    ax.invert_yaxis() # Highest at the top
    plt.tight_layout()
    plt.savefig(os.path.join(OUTPUT_DIR, "semantic_change_types.png"))
    plt.close()

# =========================
# ADVANCED INSIGHTS
# =========================
print("\n========== INSIGHTS ==========")
if semantic_percentage > 50:
    print("Majority of file updates alongside dependencies introduce semantic code changes.")
else:
    print("Most file updates alongside dependencies do NOT introduce semantic code changes.")

if not response_counts.empty:
    top_response = response_counts.idxmax()
    print(f"Most common developer response intent: '{top_response}'")

if "Bug Fix" in response_counts.index:
    print("Developers frequently perform bug fixes alongside dependency updates.")
    
if not change_df.empty:
    top_change_type = change_df.index[0]
    print(f"When a semantic change happens, the most frequent modification is: '{top_change_type}'")
print("================================\n")

print("✅ Analysis Completed Successfully! Check the 'analysis_outputs' folder for charts.")