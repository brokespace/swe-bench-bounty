import os

from task import SWEBenchTask
from dataset import SWEBenchDataset 

def run(n_tasks: int = 100, logic_files: dict = {}):
    scores = []
    dataset = SWEBenchDataset()
    for i, row in enumerate(dataset):
        if i >= n_tasks:
            break
        task = SWEBenchTask(row=row)
        score = task.run_and_score(logic_files)
        scores.append(score)
        print(f"Task {i} completed with score {score}")
    return scores

if __name__ == "__main__":
    scores = run()
    print(scores)
