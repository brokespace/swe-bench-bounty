

### Bounty Requirements


- Must submit something that is at least 2% better than the top submission on SWE-Bench Verified at the time of submission.
- There has to be a fair level of innovation in the submission. It cannot be a copy of an old submission but with a different model.

- You cannot use any self-fine-tuned models, only popular models are allowed (popular finetunes are allowed)



# Submissions

### Requirements

- You must provide a info.json with the keys: "score", and "models_used". The score should be the score receieved on swe-bench verified, and the models used should be the LLMs that you used to achieve your score.
- Your submission must follow the swe-bench submission format: https://github.com/SWE-bench/experiments/?search=1 , ie it must have trajs and logs. The submission should be done in a way that it can be directly submitted to the swe-bench leaderboard without the need to re-run anything. This means, running your submission on the entire SWE-Bench Verified dataset, to generate the trajs and the patches, then scoring the patches and storing the logs in the logs dir.

### Scoring Method

When a submission is provided it will be ran with the env var `CHUTES_ONLY` set to true, this will limit the models the api server will let you use to only those hosted by chutes. Then the validator will score it on a subset of tasks. If the score you claim is high and the score when ran with CHUTES_ONLY is also high, a manual review will occur. The manual review will consist of running your submission on the full swe-bench dataset with all models allowed. It is possible that your submission may perform well with all models but fail with chutes only models, if so you may speak to us directly about this.


Your submission must contain a main.py that is setup similar to the one in `example-submission`. The run function must have the same arguments and it must return a git patch as a string. The patch is what will be applied to the swe-bench git repo, it should fix the problem described in the problem statement.

An env var `TRAJ_DIR` is provided, any trajectories your code generates should be stored there. 