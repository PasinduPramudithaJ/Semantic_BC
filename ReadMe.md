# Semantic & Dependency Update Analyzer

This repository contains Python scripts to analyze GitHub Java projects for **dependency updates** and **semantic changes**. It tracks whether updates to `pom.xml` dependencies cause **semantic breaking changes** in the code and summarizes developer responses.

---

## Repository Structure
- ├── analyze_semantic_dependency.py # Main analysis script
- ├── analyze_results.py # Post-processing & visualization script
- ├── repos/ # Cloned GitHub repositories
- ├── analysis_outputs/ # Output CSVs and figures
- ├── results_full_semantic.csv # Full results after AST-based analysis
- └── README.md # Project documentation


---

## Features

- Clone and analyze one or more GitHub Java repositories.  
- Detect **dependency version updates** in `pom.xml`.  
- Detect **all semantic changes** using **AST parsing** of Java code (`javalang`).  
- Identify if semantic changes are caused by dependency updates.  
- Summarize developer responses (Bug Fix, Refactor, Migration, etc.).  
- Generate visualizations:  
  - Developer responses distribution  
  - Semantic vs Non-semantic breaking changes  
  - Cross-analysis of responses vs breaking changes  
- Save detailed CSV results for further research.

---

## Prerequisites

Make sure you have **Python 3.9+** installed. Also, you need **Git** to clone repositories.

Install the required Python libraries:

```bash
pip install pandas matplotlib tqdm lxml pydriller javalang
python main.py
python analyze.py

---