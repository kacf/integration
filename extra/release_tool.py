#!/usr/bin/python3

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import traceback

try:
    import yaml
except ImportError:
    print("PyYAML missing, try running 'sudo pip3 install pyyaml'.")
    sys.exit(2)

# Disable pager during menu navigation.
os.environ['GIT_PAGER'] = "cat"

# This is basically a YAML file which contains the state of the release process.
# The easiest way to understand its format is by just looking at it after the
# key fields have been filled in. This is updated continuously while the script
# is operating.
# The repositories are indexed by their Git repository names.
RELEASE_STATE = "release-state.yml"

JENKINS_SERVER = "https://ci.cfengine.com"
JENKINS_JOB = "job/yoctobuild-pr"
JENKINS_CRUMB_ISSUER = 'crumbIssuer/api/xml?xpath=concat(//crumbRequestField,":",//crumb)'

# What we use in commits messages when bumping versions.
VERSION_BUMP_STRING = "Bump versions for Mender"

# Whether or not pushes should really happen.
PUSH = True
# Whether this is a dry-run.
DRY_RUN = False

class RepoName:
    """An object that contains a pair of links for the docker and git names of a
    repository."""

    # Name of container in docker-compose file.
    container = None
    # Name of image in docker hub
    docker = None
    # Name of repository in Git. (what we index by)
    git = None
    # Whether or not this repository has a Docker container.
    has_container = None

    def __init__(self, container, docker, git, has_container):
        self.container = container
        self.docker = docker
        self.git = git
        self.has_container = has_container

# All our repos, and also a map from docker-compose image name to all
# names. The key is container name, and thereafter the order is the order of the
# RepoName constructor, just above.
#
# This is the main list of repos that will be used throughout the script. If you
# add anything here, make sure to also update REPO_ALIASES (if there are
# alternate names) and GIT_TO_BUILDPARAM_MAP (which tells the tool how to
# trigger Jenkins jobs.
REPOS = {
    "mender-api-gateway": RepoName("mender-api-gateway", "api-gateway", "mender-api-gateway-docker", True),
    "mender-client": RepoName("mender-client", "mender-client-qemu", "mender", True),
    "mender-deployments": RepoName("mender-deployments", "deployments", "deployments", True),
    "mender-device-adm": RepoName("mender-device-adm", "deviceadm", "deviceadm", True),
    "mender-device-auth": RepoName("mender-device-auth", "deviceauth", "deviceauth", True),
    "mender-gui": RepoName("mender-gui", "gui", "gui", True),
    "mender-inventory": RepoName("mender-inventory", "inventory", "inventory", True),
    "mender-useradm": RepoName("mender-useradm", "useradm", "useradm", True),

    # These ones doesn't have a Docker name, but just use same as Git for
    # indexing purposes.
    "mender-artifact": RepoName("mender-artifact", "mender-artifact", "mender-artifact", False),
    "mender-integration": RepoName("mender-integration", "integration", "integration", False),
}

# Some convenient aliases, mainly because Git phrasing differs slightly from
# Docker.
REPO_ALIASES = {
    "api-gateway-docker": "mender-api-gateway",
    "deviceadm": "mender-device-adm",
    "deviceauth": "mender-device-auth",
    "mender": "mender-client",
    "mender-client-qemu": "mender-client",
    "mender-api-gateway-docker": "mender-api-gateway",
}

# A map from git repo name to build parameter name in Jenkins.
GIT_TO_BUILDPARAM_MAP = {
    "mender-api-gateway-docker": "API_GATEWAY_REV",
    "deployments": "DEPLOYMENTS_REV",
    "deviceadm": "DEVICEADM_REV",
    "deviceauth": "DEVICEAUTH_REV",
    "gui": "GUI_REV",
    "inventory": "INVENTORY_REV",
    "useradm": "USERADM_REV",

    "mender": "MENDER_REV",
    "mender-artifact": "ARTIFACTS_REV",

    "integration": "INTEGRATION_REV",
}

# These will be saved along with the state if they are changed.
EXTRA_BUILDPARAMS = {
    "META_MENDER_REV": "pyro",
    "POKY_REV": "pyro",
    "MENDER_QA_REV": "master",
    "BUILD_QEMU": "on",
    "TEST_QEMU": "on",
    "BUILD_BBB": "on",
    "TEST_BBB": "",
    "CLEAN_BUILD_CACHE": "",
    "UPLOAD_OUTPUT": "",
    "RUN_INTEGRATION_TESTS": "on",
    "PUSH_CONTAINERS": "on",
}

def integration_dir():
    """Return the location of the integration repository."""

    if os.path.isabs(sys.argv[0]):
        return os.path.normpath(os.path.dirname(os.path.dirname(sys.argv[0])))
    else:
        return os.path.normpath(os.path.join(os.getcwd(), os.path.dirname(sys.argv[0]), ".."))

def ask(text):
    """Ask a question and return the reply."""

    sys.stdout.write(text)
    sys.stdout.flush()
    reply = sys.stdin.readline().strip()
    # Make a separator before next information chunk.
    sys.stdout.write("\n")
    return reply

def deepupdate(original, update):
    """
    Recursively update a dict.
    Subdict's won't be overwritten but also updated.
    """
    for key, value in update.items():
        if key in original and isinstance(original[key], dict) and isinstance(value, dict):
            deepupdate(original[key], value)
        else:
            original[key] = value

def determine_repo(repoish):
    """Based on a repository name, which can be any variant of Docker or Git
    name, return the Repo object assosiated with it."""

    # Try aliases first.
    alias = REPO_ALIASES.get(repoish)
    if alias is not None:
        repoish = alias

    # Prepend mender if it doesn't start with, since our index map uses that.
    if not repoish.startswith("mender-"):
        repoish = "mender-" + repoish

    return REPOS[repoish]

def docker_compose_files_list(dir):
    """Return all docker-compose*.yml files in given directory."""
    list = []
    for entry in os.listdir(dir):
        if (entry == "other-components.yml"
            or (entry.startswith("docker-compose") and entry.endswith(".yml"))):
            list.append(os.path.join(dir, entry))
    return list

def get_docker_compose_data_from_json_list(json_list):
    """Return the Yaml as data from all strings in the json list."""
    data = {}
    for json_str in json_list:
        deepupdate(data, yaml.load(json_str))
    return data

def get_docker_compose_data(dir):
    """Return the Yaml as data from all the docker-compose YAML files."""
    json_list = []
    for filename in docker_compose_files_list(dir):
        with open(filename) as fd:
            json_list.append(fd.read())

    return get_docker_compose_data_from_json_list(json_list)

def do_version_of(args):
    """Process --version-of argument."""

    data = get_docker_compose_data(integration_dir())
    try:
        repo = determine_repo(args.version_of)
    except KeyError:
        print("Unrecognized repository: %s" % args.version_of)
        sys.exit(1)
    image = data['services'][repo.container]['image']
    print(image[(image.index(":") + 1):])

def state_value(state, key_list):
    """Gets a value from the state variable stored in the RELEASE_STATE yaml
    file. The key_list is a list of indexes, where each element represents a
    subkey of the previous key.

    The difference between this function and simply indexing 'state' directly is
    that if any subkey is not found, including parent keys, None is returned
    instead of exception.
    """

    try:
        next = state
        for key in key_list:
            next = next[key]
        return next
    except KeyError:
        return None

def update_state(state, key_list, value):
    """Updates the state variable and writes this to the RELEASE_STATE state
    file. key_list is the same value as the state_value function."""
    next = state
    prev = state
    for key in key_list:
        prev = next
        if next.get(key) is None:
            next[key] = {}
        next = next[key]
    prev[key_list[-1]] = value

    fd = open(RELEASE_STATE, "w")
    fd.write(yaml.dump(state))
    fd.close()

def execute_git(state, repo_git, args, capture=False, capture_stderr=False):
    """Executes a Git command in the given repository, with args being a list
    of arguments (not including git itself). capture and capture_stderr
    arguments causes it to return stdout or stdout+stderr as a string.

    state can be None, but if so, then repo_git needs to be an absolute path.

    The function automatically takes into account Git commands with side effects
    and applies push simulation and dry run if those are enabled."""

    is_push = (args[0] == "push")
    is_change = (is_push
                 or (args[0] == "tag" and len(args) > 1)
                 or (args[0] == "branch" and len(args) > 1)
                 or (args[0] == "config" and args[1] != "-l")
                 or (args[0] == "checkout")
                 or (args[0] == "commit")
                 or (args[0] == "fetch")
                 or (args[0] == "init")
                 or (args[0] == "reset"))

    if os.path.isabs(repo_git):
        git_dir = repo_git
    else:
        git_dir = os.path.join(state['repo_dir'], repo_git)

    if (not PUSH and is_push) or (DRY_RUN and is_change):
        print("Would have executed: cd %s && git %s"
              % (git_dir, " ".join(args)))
        return None

    fd = os.open(".", flags=os.O_RDONLY)
    os.chdir(git_dir)
    if capture_stderr:
        stderr = subprocess.STDOUT
    else:
        stderr = None

    try:
        if capture:
            output = subprocess.check_output(["git"] + args, stderr=stderr).decode().strip()
        else:
            output = None
            subprocess.check_call(["git"] + args, stderr=stderr)
    finally:
        os.fchdir(fd)
        os.close(fd)

    return output

def query_execute_git_list(execute_git_list):
    """Executes a list of Git commands after asking permission. The argument is
    a list of triplets with the first three arguments of execute_git. Both
    capture flags will be false during this call."""

    print("--------------------------------------------------------------------------------")
    for cmd in execute_git_list:
        # Provide quotes around arguments with spaces in them.
        print("cd %s && git %s" % (cmd[1], " ".join(['"%s"' % str if str.find(" ") >= 0 else str for str in cmd[2]])))
    reply = ask("\nOk to execute the above commands? ")
    if not reply.startswith("Y") and not reply.startswith("y"):
        return False

    for cmd in execute_git_list:
        execute_git(cmd[0], cmd[1], cmd[2])

    return True

def query_execute_list(execute_list):
    """Executes the list of commands after asking first. The argument is a list of
    lists, where the inner list is the argument to subprocess.check_call.

    The function automatically takes into account Docker commands with side
    effects and applies push simulation and dry run if those are enabled.
    """

    print("--------------------------------------------------------------------------------")
    for cmd in execute_list:
        # Provide quotes around arguments with spaces in them.
        print(" ".join(['"%s"' % str if str.find(" ") >= 0 else str for str in cmd]))
    reply = ask("\nOk to execute the above commands? ")
    if not reply.startswith("Y") and not reply.startswith("y"):
        return False

    for cmd in execute_list:
        is_push = cmd[0] == "docker" and cmd[1] == "push"
        is_change = is_push or (
            cmd[0] == "docker" and cmd[1] == "tag")
        if (not PUSH and is_push) or (DRY_RUN and is_change):
            print("Would have executed: %s" % " ".join(cmd))
            continue

        subprocess.check_call(cmd)

    return True

def setup_temp_git_checkout(state, repo_git, branch):
    """Checks out a temporary Git directory, and returns an absolute path to
    it. Checks out the branch specified in branch."""

    tmpdir = os.path.join(state['repo_dir'], repo_git, "tmp_checkout")
    cleanup_temp_git_checkout(tmpdir)

    if branch.find('/') < 0:
        # Local branch.
        checkout_cmd = ["checkout"]
    else:
        # Remote branch.
        checkout_cmd = ["checkout", "-t"]

    execute_git(state, repo_git, ["init", "tmp_checkout"], capture=True, capture_stderr=True)
    execute_git(state, tmpdir, ["fetch", os.path.join(state['repo_dir'], repo_git),
                                "--tags", "%s:%s" % (branch, branch)], capture=True, capture_stderr=True)
    execute_git(state, tmpdir, checkout_cmd + [branch])

    return tmpdir

def cleanup_temp_git_checkout(tmpdir):
    shutil.rmtree(tmpdir, ignore_errors=True)

def find_upstream_remote(state, repo_git):
    """Given a Git repository name, figure out which remote name is the
    "mendersoftware" upstream."""

    config = execute_git(state, repo_git, ["config", "-l"], capture=True)
    remote = None
    for line in config.split('\n'):
        match = re.match(r"^remote\.([^.]+)\.url=.*github\.com[/:]mendersoftware/%s(\.git)?$"
                         % os.path.basename(repo_git), line)
        if match is not None:
            remote = match.group(1)
            break

    if remote is None:
        raise Exception("Could not find git remote pointing to mendersoftware in %s" % repo_git)

    return remote

def refresh_repos(state):
    """Do a full 'git fetch' on all repositories."""

    git_list = []

    for repo in REPOS.values():
        remote = find_upstream_remote(state, repo.git)
        git_list.append((state, repo.git, ["fetch", "--tags", remote,
                                           "+refs/heads/*:refs/remotes/%s/*" % remote]))

    query_execute_git_list(git_list)

def check_tag_availability(state):
    """Check which tags are available in all the Git repositories, and return
    this as the tag_avail data structure.

    The main fields in this one are:
      <repo>:
        already_released: <whether this is a final release tag or not (true/false)>
        build_tag: <highest build tag, or final tag>
        following: <branch we pick next build tag from>
        sha: <SHA of current build tag>
    """

    tag_avail = {}
    for repo in REPOS.values():
        tag_avail[repo.git] = {}
        try:
            execute_git(state, repo.git, ["rev-parse", state[repo.git]['version']],
                        capture=True, capture_stderr=True)
            # No exception happened during above call: This is a final release
            # tag.
            tag_avail[repo.git]['already_released'] = True
            tag_avail[repo.git]['build_tag'] = state[repo.git]['version']
        except subprocess.CalledProcessError:
            # Exception happened during Git call. This tag doesn't exist, and
            # we must look for and/or create build tags.
            tag_avail[repo.git]['already_released'] = False

            # Find highest <version>-buildX tag, where X is a number.
            tags = execute_git(state, repo.git, ["tag"], capture=True)
            highest = -1
            for tag in tags.split('\n'):
                match = re.match("^%s-build([0-9]+)$" % re.escape(state[repo.git]['version']), tag)
                if match is not None and int(match.group(1)) > highest:
                    highest = int(match.group(1))
                    highest_tag = tag
            if highest >= 0:
                # Assign highest tag so far.
                tag_avail[repo.git]['build_tag'] = highest_tag
            # Else: Nothing. This repository doesn't have any build tags yet.

        if tag_avail[repo.git].get('build_tag') is not None:
            sha = execute_git(state, repo.git, ["rev-parse", "--short",
                                                tag_avail[repo.git]['build_tag'] + "~0"],
                              capture=True)
            tag_avail[repo.git]['sha'] = sha

    return tag_avail

def repo_sort_key(repo):
    """Used in sorted() calls to sort by Git name."""
    return repo.git

def report_release_state(state, tag_avail):
    """Reports the current state of the release, including current build
    tags."""

    print("Mender release: %s" % state['version'])
    fmt_str = "%-25s %-10s %-18s %-20s"
    print(fmt_str % ("REPOSITORY", "VERSION", "PICK NEXT BUILD", "BUILD TAG"))
    print(fmt_str % ("", "", "TAG FROM", ""))
    for repo in sorted(REPOS.values(), key=repo_sort_key):
        if tag_avail[repo.git]['already_released']:
            tag = state[repo.git]['version']
            # Report released tags as following themselves, even though behind
            # the scenes we do keep track of a branch we follow. This is because
            # released repositories don't receive build tags.
            following = state[repo.git]['version']
        else:
            tag = tag_avail[repo.git].get('build_tag')
            if tag is None:
                tag = "<Needs a new build tag>"
            else:
                tag = "%s (%s)" % (tag, tag_avail[repo.git]['sha'])
            following = state[repo.git]['following']

        print(fmt_str % (repo.git, state[repo.git]['version'],
                         following, tag))

def annotation_version(repo, tag_avail):
    """Generates the string used in Git tag annotations."""

    match = re.match("^(.*)-build([0-9]+)$", tag_avail[repo.git]['build_tag'])
    if match is None:
        return "%s version %s." % (repo.git, tag_avail[repo.git]['build_tag'])
    else:
        return "%s version %s Build %s." % (repo.git, match.group(1), match.group(2))

def generate_new_tags(state, tag_avail, final):
    """Creates new build tags, and returns the new tags in a modified tag_avail. If
    interrupted anywhere, it makes no change, and returns the original tag_avail
    instead."""

    output = execute_git(state, "integration", ["show", "-s"], capture=True)
    if output.find(VERSION_BUMP_STRING) >= 0:
        # Previous version bump detected. Roll back one commit.
        execute_git(state, "integration", ["reset", "--hard", "HEAD~1"])

    # Find highest of all build tags in all repos.
    highest = 0
    for repo in REPOS.values():
        if not tag_avail[repo.git]['already_released'] and tag_avail[repo.git].get('build_tag') is not None:
            match = re.match(".*-build([0-9]+)$", tag_avail[repo.git]['build_tag'])
            if match is not None and int(match.group(1)) > highest:
                highest = int(match.group(1))

    # Assign new build tags to each repo based on our previous findings.
    next_tag_avail = copy.deepcopy(tag_avail)
    for repo in REPOS.values():
        if not tag_avail[repo.git]['already_released']:
            if final:
                # For final tag, point to the previous build tag, not the
                # version we follow.
                # "~0" is used to avoid a tag pointing to another tag. It should
                # point to the commit.
                sha = execute_git(state, repo.git, ["rev-parse", "--short",
                                                    tag_avail[repo.git]['build_tag'] + "~0"],
                                  capture=True)
                # For final tag, use actual version.
                next_tag_avail[repo.git]['build_tag'] = state[repo.git]['version']
            else:
                # For build tag, point the next tag to the last version of the
                # branch we follow.
                # "~0" is used to avoid a tag pointing to another tag. It should
                # point to the commit.
                sha = execute_git(state, repo.git, ["rev-parse", "--short",
                                                    state[repo.git]['following'] + "~0"],
                                  capture=True)
                # For non-final, use next build number.
                next_tag_avail[repo.git]['build_tag'] = "%s-build%d" % (state[repo.git]['version'], highest + 1)

            next_tag_avail[repo.git]['sha'] = sha

            print("-----------------------------------------------")
            if tag_avail[repo.git].get('build_tag') is None:
                # If there is no existing tag, just display latest commit.
                print("The latest commit in %s will be:" % repo.git)
                execute_git(state, repo.git, ["log", "-n1", sha])
            else:
                # If there is an existing tag, display range.
                print("The new commits in %s will be:" % repo.git)
                execute_git(state, repo.git, ["log", "%s..%s" % (tag_avail[repo.git]['build_tag'], sha)])
            print()

    if not final:
        print("Next build is build %d." % (highest + 1))
    print("Each repository's new tag will be:")
    report_release_state(state, next_tag_avail)

    reply = ask("Should each repository be tagged with this new build tag and pushed? ")
    if not reply.startswith("Y") and not reply.startswith("y"):
        return tag_avail

    # Create temporary directory to make changes in.
    tmpdir = setup_temp_git_checkout(state, "integration", state['integration']['following'])
    try:
        # Modify docker tags in docker-compose file.
        for repo in REPOS.values():
            set_docker_compose_version_to(tmpdir, repo.docker,
                                          next_tag_avail[repo.git]['build_tag'])

        print("-----------------------------------------------")
        print("Changes to commit:")
        print()
        execute_git(state, tmpdir, ["diff"])
        git_list = []
        git_list.append((state, tmpdir,
                         ["commit", "-a", "-s", "-m",
                          "%s %s.\n\nChangelog: None"
                          % (VERSION_BUMP_STRING, next_tag_avail["integration"]['build_tag'])]))
        if not query_execute_git_list(git_list):
            return tag_avail

        # Because of the commit above, integration repository now has a new SHA.
        sha = execute_git(state, tmpdir,
                          ["rev-parse", "--short", "HEAD~0"],
                          capture=True)
        next_tag_avail["integration"]['sha'] = sha
        # Fetch the SHA from the tmpdir to make the object available in the
        # original repository.
        execute_git(state, "integration", ["fetch", tmpdir, "HEAD"], capture=True)
    finally:
        cleanup_temp_git_checkout(tmpdir)

    # Prepare Git tag and push commands.
    git_list = []
    for repo in REPOS.values():
        if not next_tag_avail[repo.git]['already_released']:
            git_list.append((state, repo.git, ["tag", "-a", "-m", annotation_version(repo, next_tag_avail),
                                               next_tag_avail[repo.git]['build_tag'],
                                               next_tag_avail[repo.git]['sha']]))
            remote = find_upstream_remote(state, repo.git)
            git_list.append((state, repo.git, ["push", remote, next_tag_avail[repo.git]['build_tag']]))

    if not query_execute_git_list(git_list):
        return tag_avail

    # If this was the final tag, reflect that in our data.
    for repo in REPOS.values():
        if not next_tag_avail[repo.git]['already_released'] and final:
            next_tag_avail[repo.git]['already_released'] = True

    return next_tag_avail

def trigger_jenkins_build(state, tag_avail):
    try:
        import requests
    except ImportError:
        print("PyYAML missing, try running 'sudo pip3 install requests'.")
        sys.exit(2)

    # We'll be adding parameters here that shouldn't be in 'state', so make a
    # copy.
    params = copy.deepcopy(state['extra_buildparams'])

    # Populate parameters with build tags for each repository.
    postdata = []
    for repo in sorted(REPOS.values(), key=repo_sort_key):
        if tag_avail[repo.git].get('build_tag') is None:
            print("One of the repositories doesn't have a build tag yet!")
            return
        params[GIT_TO_BUILDPARAM_MAP[repo.git]] = tag_avail[repo.git]['build_tag']

    # Allow changing of build parameters.
    while True:
        print("--------------------------------------------------------------------------------")
        fmt_str = "%-25s %-20s"
        print(fmt_str % ("Build parameter", "Value"))
        for param in sorted(params.keys()):
            print(fmt_str % (param, params[param]))

        reply = ask("Will trigger a build with these values, ok? ")
        if reply.startswith("Y") or reply.startswith("y"):
            break

        reply = ask("Do you want to change any of the parameters? ")
        if not reply.startswith("Y") and not reply.startswith("y"):
            return

        name = ask("Which one? ")
        if params.get(name) is None:
            print("Parameter not found!")
            continue
        params[name] = ask("Ok. New value? ")

        if EXTRA_BUILDPARAMS.get(name) is not None:
            # Extra build parameters, that are not part of the build tags for
            # each repository, should be saved persistently in the state file so
            # that they can be repeated in subsequent builds.
            update_state(state, ['extra_buildparams', name], params[name])

    # Order is important here, because Jenkins passes in the same parameters
    # multiple times, as pairs that complete each other.
    # Jenkins additionally needs the input as json as well, so create that from
    # above parameters.
    postdata = []
    jdata = { "parameter": [] }
    for param in params.items():
        postdata.append(("name", param[0]))
        if param[1] != "":
            postdata.append(("value", param[1]))

        if param[1] == "on":
            jdata['parameter'].append({"name": param[0], "value": True})
        elif param[1] == "":
            jdata['parameter'].append({"name": param[0], "value": False})
        else:
            jdata['parameter'].append({"name": param[0], "value": param[1]})

    try:
        reply = requests.get("%s/%s" % (JENKINS_SERVER, JENKINS_CRUMB_ISSUER),
                             data=postdata)
        if reply.status_code < 200 or reply.status_code >= 300:
            print("Crumb request FAILED and returned: %d: %s" % (reply.status_code, reply.reason))
            return
        crumb = reply.content.decode().split(':', 2)
        postdata.append(("statusCode", "303"))
        jdata["statusCode"] = "303"
        postdata.append(("redirectTo", "."))
        jdata["redirectTo"] = "."
        postdata.append((crumb[0], crumb[1]))
        jdata[crumb[0]] = crumb[1]
        postdata.append(("json", json.dumps(jdata)))

        reply = requests.post("%s/%s/build?delay=0sec" % (JENKINS_SERVER, JENKINS_JOB),
                              data=postdata)
        if reply.status_code < 200 or reply.status_code >= 300:
            print("Request returned: %d: %s" % (reply.status_code, reply.reason))
        else:
            print("Build started.")
            # Crude way to find build number, pick first number starting with a
            # hash between two html tags.
            match = re.search('>#([0-9]+)<', reply.content.decode())
            if match is not None:
                print("Link: %s/%s/%s/" % (JENKINS_SERVER, JENKINS_JOB, match.group(1)))
            else:
                print("Unable to determine build number.")
    except Exception:
        print("Failed to start build:")
        traceback.print_exc()

def set_docker_compose_version_to(dir, repo_docker, tag):
    """Modifies docker-compose files in the given directory so that repo_docker
    image points to the given tag."""

    compose_files = docker_compose_files_list(dir)
    for filename in compose_files:
        old = open(filename)
        new = open(filename + ".tmp", "w")
        for line in old:
            # Replace build tag with a new one.
            line = re.sub(r"^(\s*image:\s*mendersoftware/%s:)\S+(\s*)$" % re.escape(repo_docker),
                          r"\g<1>%s\2" % tag, line)
            new.write(line)
        new.close()
        old.close()
        os.rename(filename + ".tmp", filename)

def purge_build_tags(state, tag_avail):
    """Gets rid of all tags in all repositories that match the current version
    of each repository and ends in '-build[0-9]+'. Then deletes this from
    upstream as well."""

    git_list = []
    for repo in REPOS.values():
        remote = find_upstream_remote(state, repo.git)
        tag_list = execute_git(state, repo.git, ["tag"], capture=True).split('\n')
        to_purge = []
        for tag in tag_list:
            if re.match('^%s-build[0-9]+$' % re.escape(state[repo.git]['version']), tag):
                to_purge.append(tag)
        if len(to_purge) > 0:
            git_list.append((state, repo.git, ["tag", "-d"] + to_purge))
            git_list.append((state, repo.git, ["push", remote] + [":%s" % tag for tag in to_purge]))

    query_execute_git_list(git_list)

def switch_following_branch(state, tag_avail):
    """Switches all followed branches in all repositories that aren't released,
    between local and remote branch."""

    current = None
    for repo in REPOS.values():
        if not tag_avail[repo.git]['already_released']:
            if current is None:
                # Pick first match as current state.
                current = state[repo.git]['following']
            if current.find('/') < 0:
                # Not a remote branch, switch to one.
                assign_default_following_branch(state, repo)
            else:
                # Remote branch, switch to the local one.
                local = current[(current.index('/') + 1):]
                update_state(state, [repo.git, 'following'], local)

def assign_default_following_branch(state, repo):
    remote = find_upstream_remote(state, repo.git)
    branch = re.sub(r"\.[^.]+$", ".x", state[repo.git]['version'])
    update_state(state, [repo.git, 'following'], "%s/%s" % (remote, branch))

def merge_release_tag(state, tag_avail, repo):
    """Merge tag into version branch, but only for Git history's sake, the 'ours'
    merge strategy keeps the branch as it is, the changes in the tag are not
    pulled in. Without this merge, Git won't auto-grab tags without using "git
    fetch --tags", which is inconvenient for users.
    """

    if not tag_avail[repo.git]['already_released']:
        print("Repository must have a final release tag before the tag can be merged!")
        return

    # Do the job in a temporary Git repo. Note that we check out the currently
    # followed branch, which may theoretically be later than the released tag.
    # This is because this needs to be pushed to the tip of the branch, not to
    # where the tag is.
    tmpdir = setup_temp_git_checkout(state, repo.git, state[repo.git]['following'])
    try:
        # Get a branch name for the currently checked out branch.
        branch = execute_git(state, tmpdir, ["symbolic-ref", "--short", "HEAD"],
                             capture=True)

        # Merge the tag into it.
        git_list = [((state, tmpdir, ["merge", "-s", "ours", "-m",
                                      "Merge tag %s into %s using 'ours' merge strategy."
                                      % (tag_avail[repo.git]['build_tag'], branch),
                                      tag_avail[repo.git]['build_tag']]))]
        if not query_execute_git_list(git_list):
            return

        # And then fetch that object back into the original repository, which
        # remains untouched.
        execute_git(state, repo.git, ["fetch", tmpdir, branch])

        # Push it to upstream.
        upstream = find_upstream_remote(state, repo.git)
        git_list = [((state, repo.git, ["push", upstream, "FETCH_HEAD:refs/heads/%s"
                                        % branch]))]
        if not query_execute_git_list(git_list):
            return
    finally:
        cleanup_temp_git_checkout(tmpdir)

def push_latest_docker_tags(state, tag_avail):
    """Make all the Docker ":latest" tags point to the current release."""

    for repo in REPOS.values():
        if not tag_avail[repo.git]['already_released']:
            print('You cannot push the ":latest" Docker tags without making final release tags first!')
            return

    print("This requires the versioned containers to be built and pushed already.")
    reply = ask("Has the final build finished successfully? ")
    if not reply.startswith("Y") and not reply.startswith("y"):
        return

    # Only for the message. We need to generate a new one for each repository.
    overall_minor_version = state['version'][0:state['version'].rindex('.')]

    for tip in [overall_minor_version, "latest"]:
        reply = ask('Do you want to update ":%s" tags? ' % tip)
        if not reply.startswith("Y") and not reply.startswith("y"):
            continue

        exec_list = []
        for repo in REPOS.values():
            if not repo.has_container:
                continue

            # Even though the version is already in 'tip', this is for the
            # overall Mender version. We need the specific one for the
            # repository.
            if tip == "latest":
                minor_version = "latest"
            else:
                minor_version = state[repo.git]['version'][0:state[repo.git]['version'].rindex('.')]

            exec_list.append(["docker", "pull",
                              "mendersoftware/%s:%s" % (repo.docker, tag_avail[repo.git]['build_tag'])])
            exec_list.append(["docker", "tag",
                              "mendersoftware/%s:%s" % (repo.docker, tag_avail[repo.git]['build_tag']),
                              "mendersoftware/%s:%s" % (repo.docker, minor_version)])
            exec_list.append(["docker", "push", "mendersoftware/%s:%s" % (repo.docker, minor_version)])

        query_execute_list(exec_list)

def create_release_branches(state, tag_avail):
    print("Checking if any repository needs a new branch...")

    any_repo_needs_branch = False

    for repo in REPOS.values():
        if tag_avail[repo.git]['already_released']:
            continue

        remote = find_upstream_remote(state, repo.git)

        try:
            execute_git(state, repo.git, ["rev-parse", state[repo.git]['following']],
                        capture=True, capture_stderr=True)
        except subprocess.CalledProcessError:
            any_repo_needs_branch = True
            reply = ask(("%s does not have a branch '%s'. Would you like to create it, "
                         + "and base it on latest '%s/master' (if you don't want to base "
                         + "it on '%s/master' you have to do it manually)? ")
                        % (repo.git, state[repo.git]['following'], remote, remote))
            if not reply.startswith("Y") and not reply.startswith("y"):
                continue

            cmd_list = []
            cmd_list.append((state, repo.git, ["push", remote, "%s/master:refs/heads/%s"
                                           # Slight abuse of basename() to get branch basename.
                                           % (remote, os.path.basename(state[repo.git]['following']))]))
            query_execute_git_list(cmd_list)

    if not any_repo_needs_branch:
        # Matches the beginning text above.
        print("No.")

def do_beta_to_final_transition(state):
    for repo in REPOS.values():
        version = state[repo.git]['version']
        version = re.sub("b[0-9]+$", "", version)
        update_state(state, [repo.git, 'version'], version)

    version = state['version']
    version = re.sub("b[0-9]+$", "", version)
    update_state(state, ['version'], version)

def do_release():
    """Handles the interactive menu for doing a release."""

    if os.path.exists(RELEASE_STATE):
        while True:
            reply = ask("Release already in progress. Continue or start a new one [C/S]? ")
            if reply == "C" or reply == "c":
                new_release = False
            elif reply == "S" or reply == "s":
                new_release = True
            else:
                print("Must answer C or S.")
                continue
            break
    else:
        print("No existing release in progress, starting new one...")
        new_release = True

    # Fill the state data.
    if new_release:
        state = {}
    else:
        print("Loading existing release state data...")
        print("Note that you can always edit or delete %s manually" % RELEASE_STATE)
        fd = open(RELEASE_STATE)
        state = yaml.load(fd)
        fd.close()

    if state_value(state, ['repo_dir']) is None:
        reply = ask("Which directory contains all the Git repositories? ")
        reply = re.sub("~", os.environ['HOME'], reply)
        update_state(state, ['repo_dir'], reply)

    if state_value(state, ['version']) is None:
        update_state(state, ['version'], ask("Which release of Mender will this be? "))

    update_state(state, ["integration", 'version'], state['version'])

    for repo in sorted(REPOS.values(), key=repo_sort_key):
        if state_value(state, [repo.git, 'version']) is None:
            update_state(state, [repo.git, 'version'],
                         ask("What version of %s should be included? " % repo.git))

    input = ask("Do you want to fetch all the latest tags and branches in all repositories (will not change checked-out branch)? ")
    if input.startswith("Y") or input.startswith("y"):
        refresh_repos(state)

    # Fill data about available tags.
    tag_avail = check_tag_availability(state)

    for repo in REPOS.values():
        if state_value(state, [repo.git, "following"]) is None:
            # Follow "1.0.x" style branches by default.
            assign_default_following_branch(state, repo)

    for param in EXTRA_BUILDPARAMS.keys():
        if state_value(state, ["extra_buildparams", param]) is None:
            update_state(state, ["extra_buildparams", param], EXTRA_BUILDPARAMS[param])

    create_release_branches(state, tag_avail)

    first_time = True
    while True:
        if first_time:
            first_time = False
        else:
            # Provide a break to see output from what was just done.
            ask("Press Enter... ")

        print("--------------------------------------------------------------------------------")
        print("Current state of release:")
        report_release_state(state, tag_avail)

        minor_version = state['version'][0:state['version'].rindex('.')]

        print("What do you want to do?")
        print("-- Main operations")
        if re.search("b[0-9]+$", state['version']):
            print("  O) Move from beta build tags to final build tags")
        print("  R) Refresh all repositories from upstream (git fetch)")
        print("  T) Generate and push new build tags")
        print("  B) Trigger new Jenkins build using current tags")
        print("  F) Tag and push final tag, based on current build tag")
        print('  D) Update ":%s" and/or ":latest" Docker tags to current release' % minor_version)
        print("  Q) Quit (your state is saved in %s)" % RELEASE_STATE)
        print()
        print("-- Less common operations")
        print("  P) Push current build tags (not necessary unless -s was used before)")
        print("  U) Purge build tags from all repositories")
        print('  M) Merge "integration" release tag into release branch')
        print("  S) Switch fetching branch between remote and local branch (affects next")
        print("       tagging)")
        print("  C) Create new series branch (A.B.x style) for each repository that lacks one")

        reply = ask("Choice? ")

        if reply == "Q" or reply == "q":
            break
        if reply == "R" or reply == "r":
            refresh_repos(state)
            # Refill data about available tags, since it may have changed.
            tag_avail = check_tag_availability(state)
        elif reply == "T" or reply == "t":
            tag_avail = generate_new_tags(state, tag_avail, final=False)
        elif reply == "F" or reply == "f":
            tag_avail = generate_new_tags(state, tag_avail, final=True)
            print()
            reply = ask("Purge all build tags from all repositories (recommended)? ")
            if reply == "Y" or reply == "y":
                purge_build_tags(state, tag_avail)
            reply = ask('Merge "integration" release tag into version branch (recommended)? ')
            if reply == "Y" or reply == "y":
                merge_release_tag(state, tag_avail, determine_repo("integration"))
        elif reply == "D" or reply == "d":
            push_latest_docker_tags(state, tag_avail)
        elif reply == "P" or reply == "p":
            git_list = []
            for repo in REPOS.values():
                remote = find_upstream_remote(state, repo.git)
                git_list.append((state, repo.git, ["push", remote, tag_avail[repo.git]['build_tag']]))
            query_execute_git_list(git_list)
        elif reply == "B" or reply == "b":
            trigger_jenkins_build(state, tag_avail)
        elif reply == "U" or reply == "u":
            purge_build_tags(state, tag_avail)
        elif reply == "S" or reply == "s":
            switch_following_branch(state, tag_avail)
        elif reply == "M" or reply == "m":
            merge_release_tag(state, tag_avail, determine_repo("integration"))
        elif reply == "C" or reply == "c":
            create_release_branches(state, tag_avail)
        elif reply == "O" or reply == "o":
            do_beta_to_final_transition(state)
            tag_avail = check_tag_availability(state)
        else:
            print("Invalid choice!")

def do_set_version_to(args):
    """Handles --set-version-of argument."""

    if args.version is None:
        print("--set-version-of requires --version")
        sys.exit(1)

    repo = determine_repo(args.set_version_of)
    set_docker_compose_version_to(integration_dir(), repo.docker, args.version)

def do_integration_versions_including(args):
    if not args.version:
        print("--integration-versions-including requires --version argument")
        sys.exit(2)

    git_dir = integration_dir()
    remote = find_upstream_remote(None, git_dir)
    output = execute_git(None, git_dir, ["for-each-ref", "--format=%(refname:short)",
                                         "--sort=-version:refname",
                                         "refs/tags/*",
                                         "refs/remotes/%s/master" % remote,
                                         "refs/remotes/%s/[1-9]*" % remote],
                         capture=True)
    candidates = []
    for line in output.strip().split('\n'):
        # Filter out build tags.
        if re.search("-build", line):
            continue

        candidates.append(line)

    # Now look at each docker compose file in each branch, and figure out which
    # ones contain the version of the service we are querying.
    matches = []
    for candidate in candidates:
        yamls = []
        files = execute_git(None, git_dir, ["ls-tree", "--name-only", candidate],
                            capture=True).strip().split('\n')
        for filename in files:
            if (entry != "other-components.yml"
                and not (entry.startswith("docker-compose") and filename.endswith(".yml"))):
                continue

            output = execute_git(None, git_dir, ["show", "%s:%s" % (candidate, filename)],
                                 capture=True)
            yamls.append(output)

        data = get_docker_compose_data_from_json_list(yamls)
        try:
            repo = determine_repo(args.integration_versions_including)
        except KeyError:
            print("Unrecognized repository: %s" % args.integration_versions_including)
            sys.exit(1)
        image = data['services'][repo.container]['image']
        version = image[(image.index(":") + 1):]
        if version == args.version:
            matches.append(candidate)

    for match in matches:
        print(match)

def do_verify_tag_references(args):
    int_dir = integration_dir()
    data = get_docker_compose_data(int_dir)
    problem = False

    for repo in REPOS.values():
        # integration is not checked, since the current checkout records the
        # version of that one.
        if repo.git == "integration":
            continue

        path = os.path.normpath(os.path.join(int_dir, "..", repo.git))

        try:
            tag = execute_git(None, path, ["symbolic-ref", "--short", "HEAD"], capture=True, capture_stderr=True)
            # If the above didn't produce an exception, then we are on a branch.
            # Skip the check in this case, since branches are not expected to
            # always have up to date version references.
            continue
        except subprocess.CalledProcessError:
            # We are not on a branch. Continue to next section.
            pass

        image = data['services'][repo.container]['image']
        version = image[image.rfind(':')+1:]

        # Since we already know we are not on a branch, then we should be on a
        # tag, so the next call should always succeed.
        tag = execute_git(None, path, ["describe", "--exact", "HEAD"], capture=True, capture_stderr=True)

        if tag != version:
            print("%s: Checked out tag '%s' does not match tag recorded in yml files: '%s' (from image tag: '%s')"
                  % (repo.git, tag, version, image))
            problem = True

    if problem:
        print("\nMake sure all *.yml files contain the correct versions.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-of", dest="version_of", metavar="SERVICE",
                        help="Determine version of given service")
    parser.add_argument("--set-version-of", dest="set_version_of", metavar="SERVICE",
                        help="Write version of given service into docker-compose.yml")
    parser.add_argument("--integration-versions-including", dest="integration_versions_including", metavar="SERVICE",
                        help="Find version(s) of integration repository that contain the given version of SERVICE, "
                        + " where version is given with --version. Returned as a newline separated list")
    parser.add_argument("--version", dest="version",
                        help="Version which is used in above two arguments")
    parser.add_argument("--release", action="store_true",
                        help="Start the release process (interactive)")
    parser.add_argument("-s", "--simulate-push", action="store_true",
                        help="Simulate (don't do) pushes")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Don't take any action at all")
    parser.add_argument("--verify-tag-references", action="store_true",
                        help="Checks that tag references in the yaml files match the tags that "
                        + "are checked out in Git. This is intended to catch cases where "
                        + "references to images or tools are out of date. It requires checked-out "
                        + "repositories to exist next to the integration repository, and is "
                        + "usually used only in builds. If the checked out version of a repository "
                        + "is a branch, not a tag, then the check is skipped.")
    args = parser.parse_args()

    # Check conflicting options.
    operations = 0
    for operation in [args.version_of, args.release, args.set_version_of]:
        if operation:
            operations = operations + 1
    if operations > 1:
        print("--version-of, --set-version-of and --release are mutually exclusive!")
        sys.exit(1)

    if args.simulate_push:
        global PUSH
        PUSH = False
    if args.dry_run:
        global DRY_RUN
        DRY_RUN = True

    if args.version_of is not None:
        do_version_of(args)
    elif args.set_version_of is not None:
        do_set_version_to(args)
    elif args.integration_versions_including is not None:
        do_integration_versions_including(args)
    elif args.release:
        do_release()
    elif args.verify_tag_references:
        do_verify_tag_references(args)
    else:
        parser.print_help()
        sys.exit(1)

main()
