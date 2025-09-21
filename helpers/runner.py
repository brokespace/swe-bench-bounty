import os
import json
from main import run

if __name__ == "__main__":
    repo = os.getenv("REPO")
    instance_id = os.getenv("INSTANCE_ID")
    base_commit = os.getenv("BASE_COMMIT")
    problem_statement = os.getenv("PROBLEM_STATEMENT")
    version = os.getenv("VERSION")
    traj_dir = os.getenv("TRAJ_DIR")
    repo_location = "/testbed"
    
    result = run(repo, instance_id, base_commit, problem_statement, version, repo_location)
    print("Diff: ", json.dumps({"diff": result}))
