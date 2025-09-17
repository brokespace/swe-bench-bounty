# Quickstart


## Dependencies

You must have the following things:

- Python >=3.10
- OpenRouter api key
- Github Token

## Getting started


## Installation

This repository requires python3.11, follow the commands below to install it if you do not already have it.

ONLY RUN THE FOLLOWING COMMANDS IF YOU DO NOT HAVE PYTHON INSTALLED
```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install python3.11 python3.11-venv
```

Ensure that your python version is 3.11 before continuing:
```bash
python3 --version
```

If the above doesnt return `python3.11` try using the command `python3.11` instead. If the cmd `python3.11` works, use that in place of every python command below. 

YOU WILL GET SOME ERRORS ABOUT THE PYTHON VERSION, IGNORE THEM.

After ensuring you have python run the following commands:
```bash
git clone git@github.com:brokespace/swe-bench-bounty.git
cd swe-bench-bounty
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --use-deprecated=legacy-resolver -r requirements.txt
python3 -m pip install --use-deprecated=legacy-resolver -e .
python3 -m pip uninstall uvloop # b/c it causes issues with threading/loops
```



#### Setup your dotenv

Copy `.env.template` to `.env`

#### Get a Github Token

We require github tokens, to get one follow the instructions [here](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens), or below.

1. Go to [Github](http://Github.com)
2. Open the top right menu and select `Settings`
3. Go to the bottom left and select `Developer Settings`
4. Go to either `Tokens (classic)` or `Fine-grained tokens`
5. Generate a new token and place it in the .env


#### Get a Chutes API Key

As a validator you will need to use the Chutes API to validate the miner submissions.

Instructions for getting an API Key can be found [here](https://github.com/rayonlabs/chutes?tab=readme-ov-file#-validators-and-subnet-owners).

Place the api key in the .env file like this:

```
CHUTES_API_KEY=<your chutes api key>
```


#### Get OpenRouter API Key

Place the api key in the .env file like this:

```
OPENROUTER_API_KEY=<your openrouter api key>
```



#### Install Docker

See instructions [here](https://docs.docker.com/engine/install/)

Ensure that the default user has access to the docker daemon.

```bash
sudo usermod -aG docker $USER
sudo chmod 666 /var/run/docker.sock
```

Update the `/etc/docker/daemon.json` file with the following content (localhost if youre not using a remote runner):
```bash
{
  "insecure-registries": ["<ip-of-docker-server>:5000"]
}
```

Then restart docker:

```bash
sudo systemctl restart docker
```

#### Setup LLM Server

Start the server:

```bash
source .venv/bin/activate
pm2 start --name llm-server.25000 "gunicorn llm_server:app --workers 5 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:25000 --timeout 800"
```


#### Increase Ulimit

```bash
ulimit -n unlimited
```

#### Start the validator



```bash
source .venv/bin/activate
pm2 start --name "grader" "python3 main.py"
```


