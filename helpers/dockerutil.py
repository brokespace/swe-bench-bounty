import os
import ast
import json
import time
import docker
import tempfile
import threading
from pathlib import Path

from .git import GitRepo
from .containers import DockerServer
from dataset import SWEBenchRow


def exec_container_with_timeout(container, command, timeout, log_file: str = None):
    """
    Executes a command in a Docker container with a timeout.

    Args:
        container: The Docker container object.
        command: The command to execute.
        timeout: Timeout in seconds.

    Returns:
        Tuple of exec result and logs.

    Raises:
        TimeoutError: If the command takes longer than the timeout.
    """
    exec_result = None
    logs = b""
    exception = None
    stop_event = threading.Event()

    def target():
        nonlocal exec_result, logs, exception
        try:
            exec_obj = container.exec_run(command, stream=True)
            exec_result = exec_obj.exit_code
            
            for chunk in exec_obj.output:
                if stop_event.is_set():
                    break
                logs += chunk
                print(f"\033[1;36m[{container.name}]\033[0m {chunk.decode('utf-8', errors='replace')}", end='', flush=True)
                if log_file:
                    with open(log_file, "a") as f:
                        f.write(chunk.decode('utf-8', errors='replace'))
        except Exception as e:
            exception = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        # Signal the thread to stop processing output
        stop_event.set()
        
        # Kill the container if the timeout is exceeded
        try:
            container.kill()
        except Exception as kill_exception:
            raise RuntimeError(
                f"Failed to kill the container after timeout: {kill_exception}"
            )

        raise TimeoutError(
            f"The command '{command}' exceeded the timeout of {timeout} seconds and the container was killed."
        )

    if exception:
        raise exception

    return exec_result, logs

def build_docker_container(logic_files: dict, hotkey: str, repo_files: dict) -> str:
    """
    Builds a Docker container for evaluating model logic.

    Args:
        logic_files (dict): Dictionary mapping filenames to file contents
        hotkey (str): Unique identifier for the logic
        repo_files (dict): Dictionary mapping filenames to file contents to copy to repo
        repo_path (str): Path to copy repo files to

    Returns:
        str: ID of the built container
    """
    # Initialize Docker client
    client = docker.from_env()

    # Create temporary directory to store files
    with tempfile.TemporaryDirectory() as temp_dir:
        # Write logic files to temp directory
        for filename, content in logic_files.items():
            file_path = os.path.join(temp_dir, filename)
            # Create all parent directories
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # Create the file and write content
            with open(file_path, "w", encoding="latin-1") as f:
                f.write(content)

        # Write repo files to repo path
        for filename, content in repo_files.items():
            file_path = os.path.join(temp_dir, "repo", filename)
            # Create all parent directories
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # Create the file and write content
            with open(file_path, "w", encoding="latin-1") as f:
                f.write(content)

        # Copy Dockerfile and server files
        swe_server_path = Path(__file__).parent / "swe-server"
        for item in swe_server_path.glob("*"):
            if item.is_file():
                dest_path = os.path.join(temp_dir, item.name)
                with open(item, "rb") as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
            elif item.is_dir():
                dest_dir = os.path.join(temp_dir, item.name)
                os.system(f"cp -r {item} {dest_dir}")

        # Build the container
        try:
            image, logs = client.images.build(
                path=temp_dir,
                tag=f"swe-logic-{str(hotkey)}".lower(),
                rm=True,
            )
            return image.id

        except docker.errors.BuildError as e:
            print(f"Error building container: {str(e)}")
            raise
        except docker.errors.APIError as e:
            print(f"Docker API error: {str(e)}")
            raise




def run_docker_container_from_base(
    image_name: str,
    container_name: str,
    repo: GitRepo,
    issue_description: str,
    base_commit: str,
    logic_files: dict,
    client,
    remote_host_url: str | None = None,
    volumes: dict = {},
    log_file: str = None,
    timeout: int = 1200,
    row: SWEBenchRow | None = None,
) -> dict:
    """
    Runs a Docker container for evaluating model logic.

    Args:
        container_name (str): Name of the Docker container to run
        repo (GitRepo): Git repository object containing code to evaluate
        hotkey (str): Unique identifier for the logic
        issue_description (str): Description of the issue to fix

    Returns:
        dict: The patch output from the container
    """
    # Initialize Docker client
    # container_name = f"swe-logic-{str(hotkey)}-{COMPETITION_ID}".lower()
    with tempfile.TemporaryDirectory() as temp_dir:
        code_dir = os.path.join(temp_dir, "code")
        os.makedirs(code_dir)

        # Write logic files to code directory
        for filename, content in logic_files.items():
            file_path = os.path.join(code_dir, filename)
            # Create all parent directories
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # Create the file and write content
            with open(file_path, "w", encoding="latin-1") as f:
                f.write(content)

        # Write repo files to repo path
        repo_dir = os.path.join(temp_dir, "repo")
        for filename, content in repo.files.items():
            file_path = os.path.join(repo_dir, filename)
            # Create all parent directories
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # Create the file and write content
            with open(file_path, "w", encoding="latin-1") as f:
                f.write(content)

        os.system(f"cp ./helpers/runner.py {code_dir}/runner.py")

        try:
            # try and pull the image
            try:
                client.images.pull(image_name)
            except docker.errors.APIError as e:
                print(f"Error pulling image: {str(e)}")
                

            # Remove any existing container with the same name
            try:
                existing = client.containers.get(container_name)
                existing.remove(force=True)
            except docker.errors.NotFound:
                pass

            container = client.containers.create(
                image=image_name,
                name=container_name,
                detach=True,
                # ports={"3000/tcp": 3000},
                network_mode="host",
                extra_hosts={"host.docker.internal": "host-gateway"},
                environment={
                    "HOST_IP": os.getenv("HOST_IP", "localhost"),
                    "REPO": row.repo,
                    "INSTANCE_ID": row.instance_id,
                    "BASE_COMMIT": row.base_commit,
                    "PROBLEM_STATEMENT": row.problem_statement,
                    "VERSION": row.version,
                    "REPO_LOCATION": "/testbed",
                    "TRAJ_DIR": "/app/code/trajs",
                    "CHUTES_ONLY": os.getenv("CHUTES_ONLY", "true")
                },
                command="sleep infinity",
                volumes=volumes,
            )

            # Start the container
            container.start()
            container.exec_run(f"git reset --hard {base_commit}", workdir="/testbed")
            # Copy files from temp_dir into container
            if remote_host_url:
                # For remote Docker host, use docker context or SSH to copy files
                os.system(
                    f"docker -H {remote_host_url} cp {temp_dir}/code/. {container_name}:/app/code/"
                )
                # Clear /testbed/ directory before copying new files
                # container.exec_run("rm -rf /testbed/*")
                # Copy repo files to /testbed/ directory
                # os.system(
                    # f"docker -H {remote_host_url} cp {temp_dir}/repo/. {container_name}:/testbed/"
                # )
            else:
                # For local Docker host
                os.system(f"docker cp {temp_dir}/code/. {container_name}:/app/code/")
                # Clear /testbed/ directory before copying new files
                # container.exec_run("rm -rf /testbed/*")
                # Copy repo files to /testbed/ directory
                # os.system(f"docker cp {temp_dir}/repo/. {container_name}:/testbed/")

            # run the requirements.txt in the code directory
            container.exec_run("pip install -r /app/code/requirements.txt")
            container.exec_run("mkdir -p /app/code/trajs")
            
            # Execute runner.py in container
            exec_result, logs = exec_container_with_timeout(
                container, "python3 -u /app/code/runner.py", timeout, log_file=log_file
            )
            logs = logs.decode("utf-8")
            # print(f"===== CONTAINER {container_name} LOGS =====")
            # print(logs)
            # print(f"===== END OF CONTAINER {container_name} LOGS =====")
            # Look for either Patch or Diff in the logs
            for line in reversed(logs.split("\n")):
                if line.startswith("Patch:"):
                    try:
                        # First try parsing as JSON
                        patch_dict = json.loads(line.replace("Patch:", "").strip())
                    except json.JSONDecodeError:
                        # Fall back to safely evaluating as literal Python dict
                        patch_dict = ast.literal_eval(line.replace("Patch:", "").strip())
                    return patch_dict
                elif line.startswith("Diff:"):
                    try:
                        # Parse the diff JSON
                        diff_dict = json.loads(line.replace("Diff:", "").strip())
                        return diff_dict['diff']
                    except json.JSONDecodeError:
                        # Fall back to safely evaluating as literal Python dict
                        diff_dict = ast.literal_eval(line.replace("Diff:", "").strip())
                        return diff_dict['diff']
            # print(f"===== CONTAINER {container_name} LOGS =====")
            # print(logs)
            # print(f"===== END OF CONTAINER {container_name} LOGS =====")
            # If we get here, neither Patch nor Diff was found
            raise ValueError("No Patch or Diff found in container logs")

        except docker.errors.APIError as e:
            print(f"Docker API error: {str(e)}")
            raise

        finally:
            # Cleanup container
            try:
                container.stop(timeout=1)
            except:
                pass

            try:
                container.remove(force=True)
            except:
                pass

def test_docker_container(remote_host_url: str):
    docker_server = DockerServer(remote_host_url=remote_host_url, remote_host_registry=f"{os.getenv('DOCKER_HOST_IP')}:5000")
    docker_server.load_image_remote("brokespace/swe-server:latest")
    container_name = "swe-server"
    with tempfile.TemporaryDirectory() as temp_dir:
        code_dir = os.path.join(temp_dir, "code")
        os.makedirs(code_dir)

        # Copy Dockerfile and server files
        swe_server_path = Path(__file__).parent / "swe-server"
        for item in swe_server_path.glob("*"):
            if item.is_file():
                dest_path = os.path.join(code_dir, item.name)
                with open(item, "rb") as src, open(dest_path, "wb") as dst:
                    dst.write(src.read())
            elif item.is_dir():
                dest_dir = os.path.join(code_dir, item.name)
                os.system(f"cp -r {item} {dest_dir}")

        try:
            # Remove any existing container with the same name
            try:
                existing = docker_server._remote_client.containers.get(container_name)
                existing.remove(force=True)
            except docker.errors.NotFound:
                pass

            container = docker_server._remote_client.containers.create(
                image=f"{os.getenv('DOCKER_HOST_IP')}:5000/brokespace/swe-server:latest",
                name=container_name,
                detach=True,
                # ports={"3000/tcp": 3000},
                extra_hosts={"host.docker.internal": "host-gateway"},
                environment={"HOST_IP": os.getenv("HOST_IP", "localhost"), "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", "")},
                command="sleep infinity",
            )
            

            # Start the container
            container.start()

            # Copy files from temp_dir into container using the remote Docker host
            docker_cp_cmd = (
                f"docker -H {remote_host_url} cp {temp_dir}/. {container_name}:/app/"
            )
            os.system(docker_cp_cmd)

            # Execute runner.py in container
            exec_result, logs = exec_container_with_timeout(
                container, "python3 -u /app/code/test.py", 600
            )
            logs = logs.decode("utf-8")
            print("===== TEST CONTAINER LOGS =====")
            print(logs)
            print("===== TEST CONTAINER LOGS =====")
            if "The test passed" in logs:
                return True
            else:
                return False

        except docker.errors.APIError as e:
            print(f"Docker API error: {str(e)}")
            raise

        finally:
            # Cleanup container
            try:
                container.stop(timeout=1)
            except:
                pass

            try:
                container.remove(force=True)
            except:
                pass


def delete_all_containers(remote_host_url: str | None = None):
    client = (
        docker.from_env()
        if remote_host_url is None
        else docker.DockerClient(base_url=remote_host_url)
    )
    for container in client.containers.list():
        if "registry" not in container.name:
            try:
                container.stop(timeout=1)
                container.remove(force=True)
            except Exception as e:
                print(f"Error deleting container: {container.name} - {e}")


def exec_run_with_timeout(container, cmd, timeout: int | None = 60):
    """
    Run a command in a container with a timeout.

    Args:
        container (docker.Container): Container to run the command in.
        cmd (str): Command to run.
        timeout (int): Timeout in seconds.
    """
    # Local variables to store the result of executing the command
    exec_result = b""
    exec_id = None
    exception = None
    timed_out = False

    # Wrapper function to run the command
    def run_command():
        nonlocal exec_result, exec_id, exception
        try:
            exec_id = container.client.api.exec_create(container.id, cmd)["Id"]
            exec_stream = container.client.api.exec_start(exec_id, stream=True)
            for chunk in exec_stream:
                exec_result += chunk
        except Exception as e:
            exception = e

    # Start the command in a separate thread
    thread = threading.Thread(target=run_command)
    start_time = time.time()
    thread.start()
    thread.join(timeout)

    if exception:
        raise exception

    # If the thread is still alive, the command timed out
    if thread.is_alive():
        if exec_id is not None:
            exec_pid = container.client.api.exec_inspect(exec_id)["Pid"]
            container.exec_run(f"kill -TERM {exec_pid}", detach=True)
        timed_out = True
    end_time = time.time()
    return exec_result.decode(errors="ignore"), timed_out, end_time - start_time


if __name__ == "__main__":
    import dotenv

    dotenv.load_dotenv()
    print(test_docker_container())
