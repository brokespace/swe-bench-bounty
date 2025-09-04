from datasets import load_dataset
from pydantic import BaseModel


class SWEBenchRow(BaseModel):
    repo: str
    instance_id: str
    base_commit: str
    patch: str
    test_patch: str
    problem_statement: str
    version: str
    environment_setup_commit: str
    full_dict: dict
    

class SWEBenchDataset:
    def __init__(self):
        self.dataset = load_dataset("princeton-nlp/SWE-bench", split="test", streaming=True)

    def __len__(self):
        return len(self.dataset)
    
    def __iter__(self):
        for row in self.dataset:
            yield SWEBenchRow(**row, full_dict=row)
    
    def __next__(self):
        data = next(self.dataset)
        return SWEBenchRow(**data, full_dict=data)