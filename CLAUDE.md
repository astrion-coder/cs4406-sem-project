# Reference Guide for Claude Code
This document contains the necessary information about the project that will allow Claude Code to help with the project.

# Important Files and Folders
1. `Project_Idea.md` - This is the project we are planning to implement
2. `src/` - This is the directory where the main code of the project goes.
3. `notebooks/` - This should contain only .ipynb files that are necessary for remote training on external GPUs like kaggle and for evaluation numbers. When you want to evaluate like the latency or some other kind of numeric value, place a quick script here and let me verify the numbers.

# Tech Stack
The programming language is python. Package management is maintained by uv.

# Other instructions
1. You are not allowed to install any kind of software. If installation is necessary, you stop and tell me what to install. Even simple `uv add` instructions are not allowed.
2. Anytime a numeric claim is made, it should be referenced in README.md with the file and the cell number that indicates which cell the numeric claim was made from. I want to run that cell again to verify the claims.