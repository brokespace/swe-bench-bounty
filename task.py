from dotenv import load_dotenv
load_dotenv()
import os
import time
import docker
import logging
import tempfile
import traceback
import asyncio
import base64
import time
from models import SubmissionData, SubmissionType
from dataset import SWEBenchRow, SWEBenchDataset
from pathlib import Path, PurePosixPath
from helpers.containers import DockerServer
from helpers.dockerutil import run_docker_container_from_base, exec_run_with_timeout
from helpers.git import GitRepo

from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    KEY_PREDICTION,
    LOG_TEST_OUTPUT,
    UTF8,
)
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
)
from swebench.harness.docker_build import (
    BuildImageError,
)
from swebench.harness.grading import get_eval_report
from swebench.harness.utils import (
    EvaluationError,
)


GIT_APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --reject",
    "patch --batch --fuzz=8 -p1 -l",
]


def run_instance(
    repo: GitRepo,
    instance: dict,
    pred: dict,
    rm_image: bool,
    force_rebuild: bool,
    client: docker.DockerClient,
    run_id: str,
    timeout: int | None = None,
    image_name: str = None,
):
    """
    Run a single instance with the given prediction.

    Args:
        test_spec (TestSpec): TestSpec instance
        pred (dict): Prediction w/ model_name_or_path, model_patch, instance_id
        rm_image (bool): Whether to remove the image after running
        force_rebuild (bool): Whether to force rebuild the image
        client (docker.DockerClient): Docker client
        run_id (str): Run ID
        timeout (int): Timeout for running tests
    """
    test_spec = make_test_spec(
        instance, namespace="swebench", instance_image_tag="latest"
    )
    # Set up logging directory
    instance_id = test_spec.instance_id
    logger = logging.getLogger()
    with tempfile.NamedTemporaryFile(delete=False) as temp_log_file:
        setattr(logger, "log_file", temp_log_file.name)
    # Run the instance
    container = None
    try:
        print(f"Creating container for {instance_id} from image {image_name}...")
        # Check if container with this name already exists and remove it
        try:
            existing_container = client.containers.get(instance_id)
            print(f"Container {instance_id} already exists, removing it...")
            existing_container.remove(force=True)
        except docker.errors.NotFound:
            pass  # Container doesn't exist, which is fine
        # Create and start instance container from the existing image
        container = client.containers.create(
            image=image_name, name=instance_id, command="tail -f /dev/null"
        )
        container.start()
        print(f"Container for {instance_id} created and started: {container.id}")
        # Delete everything in /testbed/ and copy new files
        # container.exec_run("rm -rf /testbed/*")
        # os.system(f"docker cp {repo.temp_dir}/. {instance_id}:/testbed/")
        container.exec_run(
            f"git reset --hard {instance['base_commit']}", workdir="/testbed"
        )
        container.exec_run(
            "git config --global --add safe.directory /testbed", workdir="/testbed"
        )

        

        # Create a temporary directory
        with tempfile.TemporaryDirectory() as log_dir:
            log_dir = Path(log_dir)
            # Copy model prediction as patch file to container
            patch_file = log_dir / "patch.diff"
            # pred[KEY_PREDICTION] = pred[KEY_PREDICTION]
            patch_file.write_text(pred[KEY_PREDICTION] or "")
            # print(
            # f"Intermediate patch for {instance_id} written to {patch_file}, now applying to container..."
            # )
            copy_to_container(container, patch_file, PurePosixPath(DOCKER_PATCH))
            # print("THE PATCH is: ", pred[KEY_PREDICTION].split("\n"))
            # print(container.exec_run("ls", workdir="/testbed", user="root").output.decode(UTF8))
            # Attempt to apply patch to container (TODO: FIX THIS)
            applied_patch = False
            for git_apply_cmd in GIT_APPLY_CMDS:
                val = container.exec_run(
                    f"{git_apply_cmd} {DOCKER_PATCH}",
                    workdir=DOCKER_WORKDIR,
                    user=DOCKER_USER,
                )
                if val.exit_code == 0:
                    # print(f"{APPLY_PATCH_PASS}:\n{val.output.decode(UTF8)}")
                    applied_patch = True
                    break
                # else:
                    # print(f"Failed to apply patch to container: {git_apply_cmd}")
                    # print("The error is: ", val.output.decode(UTF8))
                    # print("The patch is: ", pred[KEY_PREDICTION])

            if not applied_patch:
                print(f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}")
                raise EvaluationError(
                    instance_id,
                    f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}",
                    logger,
                )
            # Get git diff before running eval script
            git_diff_output_before = (
                container.exec_run(
                    "git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR
                )
                .output.decode(UTF8)
                .strip()
            )

            eval_file = Path(log_dir / "eval.sh")
            eval_file.write_text(test_spec.eval_script)
            # print(
            #     f"Eval script for {instance_id} written to {eval_file}; copying to container..."
            # )
            copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))
            # Run eval script, write output to logs
            test_output, timed_out, total_runtime = exec_run_with_timeout(
                container, "/bin/bash /eval.sh", timeout
            )
            test_output_path = log_dir / LOG_TEST_OUTPUT
            print(f"Test runtime: {total_runtime:_.2f} seconds")
            with open(test_output_path, "w") as f:
                f.write(test_output)
                # print(f"Test output for {instance_id} written to {test_output_path}")
                if timed_out:
                    f.write(f"\n\nTimeout error: {timeout} seconds exceeded.")
                    raise EvaluationError(
                        instance_id,
                        f"Test timed out after {timeout} seconds.",
                        logger,
                    )

            # Get git diff after running eval script (ignore permission changes)
            git_diff_output_after = (
                container.exec_run(
                    "git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR
                )
                .output.decode(UTF8)
                .strip()
            )

            # Get report from test output
            print(f"Grading answer for {instance_id}...")
            report = get_eval_report(
                test_spec=test_spec,
                prediction=pred,
                test_log_path=test_output_path,
                include_tests_status=True,
            )
        return instance_id, report
    except EvaluationError as e:
        error_msg = traceback.format_exc()
        print(error_msg)
        print(e)
    except BuildImageError as e:
        error_msg = traceback.format_exc()
        print(error_msg)
        print(e)
    except Exception as e:
        error_msg = (
            f"Error in evaluating model for {instance_id}: {e}\n"
            f"{traceback.format_exc()}\n"
        )
        print(error_msg)
    finally:
        # Remove instance container + image, close logger
        cleanup_container(client, container, logger)

    return


def score_patch(
    patch: str, repo: GitRepo, instance: dict, client: docker.DockerClient, image_name: str
):
    # if patch.strip() == "":
        # return 0

    prediction = {
        "instance_id": instance["instance_id"],
        "model_patch": patch,
        "raw_model_patch": patch,
        "model_name_or_path": "gpt-4o",
        "original_file_content": "",
    }
    try:
        result = run_instance(
            repo, instance, prediction, False, False, client, "nil", 600, image_name
        )
        if result[1][instance["instance_id"]]["resolved"]:
            return 1
        else:
            return 0
    except Exception as e:
        print("There was an error scoring the patch: ", e)
        print(traceback.format_exc())
        return 0

def normalize_image_name(image_name):
    import re
    # Find all IP:port patterns
    ip_port_pattern = r'\d+\.\d+\.\d+\.\d+:\d+'
    matches = re.findall(ip_port_pattern, image_name)
    
    # If there are multiple IP:port matches, keep only the first one
    if len(matches) > 1:
        for match in matches[1:]:
            image_name = image_name.replace(match + '/', '')
            image_name = image_name.replace(match, '')
    
    return image_name

class SWEBenchTask:
    def __init__(self, row: SWEBenchRow, use_remote: bool = True, logger_func=None):
        self.row = row
        self.use_remote = use_remote
        self.docker_server = DockerServer(
                remote_host_url=os.getenv("REMOTE_DOCKER_HOST", None),
                remote_host_registry=f"{os.getenv('DOCKER_HOST_IP', None)}:5000",
            )
        self.repo = GitRepo(self.row.repo, self.row.base_commit)
        docker_host_ip = os.getenv("DOCKER_HOST_IP")
        image_name = f"swe-eval-{self.row.repo}-{self.row.version}"
        normalized_name = normalize_image_name(image_name)
        self.image_name = f"{docker_host_ip}:5000/{normalized_name}"
        self.log = logger_func
    
    def _build_image(self):
        test_spec = make_test_spec(
            self.row.full_dict, namespace="swebench", instance_image_tag="latest"
        )

        # Check if image already exists
        client = (
            self.docker_server._local_client
            if not self.use_remote or not self.docker_server.remote
            else self.docker_server._remote_client
        )

        try:
            client.images.get(self.image_name)
            print(f"Image {self.image_name} already exists, skipping build")
            return
        except:
            print(f"Building image {self.image_name}")
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_script = test_spec.install_repo_script.replace("reset --hard", "checkout -f")
            with open(os.path.join(temp_dir, "setup_repo.sh"), "w") as f:
                f.write(repo_script)
            dockerfile_content = f"""
FROM "brokespace/swe-env-{test_spec.repo.replace("/", "-")}-{test_spec.version}"
USER root

COPY ./setup_repo.sh /root/
RUN sed -i -e 's/\r$//' /root/setup_repo.sh
RUN /bin/bash /root/setup_repo.sh

WORKDIR /testbed/
"""
            with open(os.path.join(temp_dir, "Dockerfile"), "w") as f:
                f.write(dockerfile_content)
            start_time = time.time()
            for _ in range(3): # try 3 times
                try:
                    if (
                        self.use_remote
                        and hasattr(self.docker_server, "remote")
                        and self.docker_server.remote
                    ):
                        self.docker_server.remote.build(
                            path=temp_dir, tag=self.image_name, push=False
                        )
                    else:
                        self.docker_server.local.build(path=temp_dir, tag=self.image_name)
                    break
                except Exception as e:
                    print("There was an error building the image: ", e)
                    print(traceback.format_exc())
            if self.use_remote and hasattr(self.docker_server, "remote") and self.docker_server.remote:
                self.docker_server._remote_client.images.pull(self.image_name)
            end_time = time.time()
            build_duration = end_time - start_time
            print(f"Building the Docker image took {build_duration:.2f} seconds.")
    
    def __getstate__(self):
        # Remove the Docker image before pickling
        # self.client.images.remove(image=self.image_name, force=True)
        state = self.__dict__.copy()
        state["docker_server"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        # Rebuild the Docker image after unpickling
        self.docker_server = DockerServer(
            remote_host_url=os.getenv("REMOTE_DOCKER_HOST", None),
            remote_host_registry=f"{os.getenv('DOCKER_HOST_IP', None)}:5000",
        )
        if (
            self.use_remote
            and hasattr(self.docker_server, "remote")
            and self.docker_server.remote
            and os.getenv("DOCKER_HOST_IP") not in self.image_name
        ):
            docker_host_ip = os.getenv("DOCKER_HOST_IP")
            self.image_name = f"{docker_host_ip}:5000/{self.image_name}"
        # Extract the version part from the image name, handling multiple colons
        # image_parts = self.image_name.split(":")
        # if len(image_parts) > 1 and image_parts[-1] != "IMAGE_VERSION":
            # self.image_name = ":".join(image_parts[:-1]) + ":" + IMAGE_VERSION
        
        self._build_image()

    def run_and_score(self, logic: dict[str, str]) -> int:
        client = self.docker_server._remote_client if self.use_remote else self.docker_server._local_client 
        self._build_image()
        try:
            result = run_docker_container_from_base(
                image_name=self.image_name,
                container_name=f"swe-logic-{self.row.instance_id}",
                repo=self.repo,
                issue_description=self.row.problem_statement,
                base_commit=self.row.base_commit,
                logic_files=logic,
                client=self.docker_server._remote_client if self.use_remote else self.docker_server._local_client,
                remote_host_url=os.getenv("REMOTE_DOCKER_HOST", None),
                row=self.row,
                log=self.log
            )
            return score_patch(result, self.repo, self.row.full_dict, client, self.image_name)
        except Exception as e:
            print("There was an error running the task: ", e)
            print(traceback.format_exc())
            return 0

    def _cleanup(self):
        self.repo._cleanup()
        try:
            client = self.docker_server._remote_client if self.use_remote else self.docker_server._local_client
            # Remove all images
            images = client.images.list()
            for image in images:
                try:
                    client.images.remove(image.id, force=True)
                except Exception as e:
                    pass
        except Exception as e:
            pass
    
    def cleanup(self):
        self._cleanup()

    def load_logic(self, path: str):
        logic = {}
        for root, dirs, files in os.walk(path):
            # Skip __pycache__ directories
            if "__pycache__" in dirs:
                dirs.remove("__pycache__")

            # Get relative path from path
            rel_path = os.path.relpath(root, path)

            # Process all files in current directory
            for filename in files:
                # Skip __pycache__ files
                if "__pycache__" in filename:
                    continue

                file_path = os.path.join(root, filename)
                # Get the relative path for the logic dict key
                if rel_path == ".":
                    logic_key = filename
                else:
                    logic_key = os.path.join(rel_path, filename)

                with open(file_path, "r", encoding="latin-1") as f:
                    logic[logic_key] = f.read()
        
        return logic

class BountyTask:
    def __init__(self, job_id: str, logger_func=None):
        self.job_id = job_id
        self.logger_func = logger_func
        self.dataset = SWEBenchDataset()
        self.tasks = []
    
    def log(self, level: str, message: str, **kwargs):
        if self.logger_func:
            if asyncio.iscoroutinefunction(self.logger_func):
                asyncio.run(self.logger_func(level, message, self.job_id, **kwargs))
            else:
                self.logger_func(level, message, self.job_id, **kwargs)

    def load_tasks(self):
        for i, row in enumerate(self.dataset):
            if i >= int(os.getenv("TASK_COUNT", 100)):
                break
            task = SWEBenchTask(row=row, use_remote=False, logger_func=self.log)
            self.tasks.append(task)

    async def score(self, submission: SubmissionData) -> float:
        self.log("info", "Loading tasks")
        self.load_tasks()
        git_repo_url = submission.content
        submission_repo = GitRepo(git_repo_url)
        scores = []
        for task in self.tasks:
            self.log("info", f"Scoring task {task.row.instance_id}")
            scores.append(task.run_and_score(task.load_logic(submission_repo.path)))
            self.log("info", f"Scored task {task.row.instance_id} with score {scores[-1]}")
        return sum(scores) / len(scores)
    
    def cleanup(self):
        for task in self.tasks:
            task.cleanup()



if __name__ == "__main__":
    bounty_task = BountyTask(job_id="test")
    try:
        submission_data = SubmissionData(
            job_id="test",
            submission_id="test_submission",
            submission_type=SubmissionType.LINK,
            content="https://github.com/brokespace/sample-swebench-repo"
        )
        print(asyncio.run(bounty_task.score(submission_data)))
    except:
        pass
    finally:
        bounty_task.cleanup()