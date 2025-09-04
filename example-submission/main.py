import os
import json
from proxy import ProxyClient   

CHUTES_ONLY = os.getenv("CHUTES_ONLY", "true").lower() == "true"
TRAJ_DIR = os.getenv("TRAJ_DIR", "./trajs")

client = ProxyClient()

def run(
    repo: str,
    instance_id: str,
    base_commit: str,
    problem_statement: str,
    version: str,
    repo_location: str
) -> str:
    if CHUTES_ONLY:
        pass
        # only use models that are hosted by chutes
    else:
        pass
        # use any model on openrouter
    
    # for any llm call store the trajectory in the TRAJ_DIR
    response = client.llm(
        model="deepseek-ai/DeepSeek-V3-0324",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you?"}
        ]
    )
    with open(os.path.join(TRAJ_DIR, "trajectory.json"), "w") as f:
        json.dump(response.model_dump(), f)
    return "git patch"