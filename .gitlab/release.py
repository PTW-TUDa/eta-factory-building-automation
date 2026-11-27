#!/usr/bin/env python3
"""Prepare and validate GitLab release notes for TwinCAT project releases.

The normal workflow edits the release MR in GitLab's UI. Only update-mr and
publish write to GitLab; draft and validation commands are read-only there.
github-snapshot only builds the public commit for review; github-publish pushes
exactly that reviewed commit and creates a draft GitHub release.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
ZERO = "0" * 40
VERSION_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
START_MARKER = "<!-- release-notes:start -->"
END_MARKER = "<!-- release-notes:end -->"
# Paths removed from the public GitHub snapshot. Any other *.TcGVL is an error.
PUBLIC_EXCLUDE = ("ETA_Fabrik_C6030/CP_5C7432/GVLs",)
PRIVATE_FILE_RE = re.compile(r"(?i)\.tcgvl\Z")
CATEGORIES = dict(re.findall(
    r"(?m)^  (breaking|feature|bug|internal): ([A-Za-z ]+)$",
    (ROOT / ".gitlab/changelog_config.yml").read_text(encoding="utf-8"),
))
if set(CATEGORIES) != {"breaking", "feature", "bug", "internal"}:
    raise ValueError("changelog_config.yml must define all four categories")


class ReleaseError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def git(*args, env=None):
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, encoding="utf-8",
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, **env} if env else None)
    if result.returncode:
        raise ReleaseError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def git_ok(*args):
    return subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def version():
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    require(VERSION_RE.fullmatch("v" + value), f"Invalid VERSION: {value!r}")
    return value


def validate_versions():
    # A TwinCAT project (not a library): no ProjectVersion or .library to check.
    expected = version()
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    matches = re.findall(r"(?m)^version:\s*(\S+)\s*$", citation)
    require(matches == ["v" + expected], f"CITATION.cff version must be v{expected}")
    return expected


def parse_changes(description):
    """Read individual bullets from an eta-ctrl-style MR description."""
    lines = description.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "## Release notes"]
    require(len(starts) == 1, "MR needs exactly one '## Release notes' section")
    items = []
    category = None
    current = None
    for line in lines[starts[0] + 1:]:
        if line.startswith("## "):
            break
        if not line.strip():
            continue
        if line.startswith("<!--") or line.startswith("-->"):
            raise ReleaseError("Remove release-note template instructions")
        if line.startswith("### "):
            heading = line[4:].strip()
            matches = [key for key, title in CATEGORIES.items() if title == heading]
            require(len(matches) == 1, f"Unknown release-note category {heading!r}")
            category = matches[0]
            current = None
            continue
        if line.startswith("- "):
            require(category is not None, "Change bullet needs a category heading")
            change = line[2:].strip()
            require(change and not change.startswith("Describe "), "Fill in each change bullet")
            current = {"category": category, "change": change, "compatibility": "", "migration": ""}
            items.append(current)
            continue
        detail = re.fullmatch(r"  (Compatibility|Migration):\s*(.*)", line)
        require(detail and current is not None and current["category"] == "breaking",
                f"Unexpected release-note line: {line!r}")
        field = detail.group(1).lower()
        require(not current[field], f"Duplicate {field} for breaking change")
        current[field] = detail.group(2).strip()
    require(items, "Add at least one release-note bullet")
    for item in items:
        if item["category"] == "breaking":
            for field in ("compatibility", "migration"):
                value = item[field]
                require(value and not value.startswith(("Describe ", "Give concrete", "None")),
                        f"Breaking change {item['change']!r} needs {field} text")
    return items


def previous_release(main, before=None):
    tags = []
    limit = tuple(map(int, VERSION_RE.fullmatch(before).groups())) if before else None
    for tag in git("tag", "--list", "v*").splitlines():
        match = VERSION_RE.fullmatch(tag)
        numbers = tuple(map(int, match.groups())) if match else None
        if numbers and (limit is None or numbers < limit):
            sha = git("rev-parse", tag + "^{commit}")
            if git_ok("merge-base", "--is-ancestor", sha, main):
                tags.append((numbers, tag, sha))
    return max(tags)[1:] if tags else (None, ZERO)


def require_new_version(main):
    previous, _ = previous_release(main)
    if previous:
        current_numbers = tuple(map(int, VERSION_RE.fullmatch("v" + version()).groups()))
        previous_numbers = tuple(map(int, VERSION_RE.fullmatch(previous).groups()))
        require(current_numbers > previous_numbers,
                f"VERSION must be newer than the previous stable tag {previous}")


def source_commits(start, end):
    git("cat-file", "-e", end + "^{commit}")
    if start == ZERO:
        revision = end
    else:
        git("cat-file", "-e", start + "^{commit}")
        boundary = git("merge-base", start, end)
        require(boundary, f"No shared history between previous release {start} and development {end}")
        revision = f"{boundary}..{end}"
    return git("rev-list", "--first-parent", "--reverse", revision).splitlines()


def entries(start, end, project_url):
    project_url = project_url.rstrip("/")
    require(project_url.startswith(("https://", "http://")), "Provide --project-url")
    result = []
    seen_mrs = set()
    for sha in source_commits(start, end):
        parents = git("rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) < 3:  # Direct development commits are deliberately omitted.
            continue
        message = git("show", "-s", "--format=%B", sha)
        if message.splitlines()[0] == "Merge branch 'master' into 'development'":
            continue
        if "## Release notes" not in message:
            # The first release ignores merges predating this workflow. Once the
            # template exists on the first parent, missing metadata is an error.
            if git_ok("cat-file", "-e", f"{parents[1]}:.gitlab/merge_request_templates/Feature.md"):
                raise ReleaseError(f"Merge {sha[:12]} has no '## Release notes' section")
            continue
        changes = parse_changes(message)
        refs = re.findall(r"(?m)^See merge request (?:\S+)?!(\d+)\s*$", message)
        require(len(refs) == 1, f"Merge {sha[:12]} needs exactly one GitLab MR reference")
        iid = refs[0]
        require(iid not in seen_mrs, f"Duplicate merge request !{iid}")
        seen_mrs.add(iid)
        link = f"{project_url}/-/merge_requests/{iid}"
        for number, change in enumerate(changes, 1):
            result.append({**change, "key": f"!{iid}#{number}", "link": link})
    return result


def draft(start, end, project_url, release_version):
    items = entries(start, end, project_url)
    lines = [START_MARKER, f"## v{release_version}", ""]
    for category, heading in CATEGORIES.items():
        group = [item for item in items if item["category"] == category]
        if not group:
            continue
        lines.extend([f"### {heading}", ""])
        for item in group:
            lines.append(f"- {item['change']} ([{item['key']}]({item['link']}))")
            if category == "breaking":
                lines.append(f"  Compatibility: {item['compatibility']}")
                lines.append(f"  Migration: {item['migration']}")
        lines.append("")
    if not items:
        lines.extend(["No changes.", ""])
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def approved_notes(description, release_version):
    require(description.count(START_MARKER) == description.count(END_MARKER) == 1,
            "Release MR needs exactly one marked release-note block")
    start = description.index(START_MARKER) + len(START_MARKER)
    end = description.index(END_MARKER)
    require(end > start, "Release-note markers are out of order")
    notes = description[start:end].strip() + "\n"
    require(notes.startswith(f"## v{release_version}\n"),
            f"Approved notes must start with ## v{release_version}")
    return notes


def insert_notes(original, draft_text):
    notes = draft_text.strip()
    require(notes.startswith(START_MARKER) and notes.endswith(END_MARKER),
            "Notes file needs release-note markers")
    if START_MARKER in original or END_MARKER in original:
        require(START_MARKER in original and END_MARKER in original and
                original[original.index(START_MARKER):original.index(END_MARKER) + len(END_MARKER)] == notes,
                "MR notes were edited; update them in GitLab UI instead of overwriting")
        return original, False
    return (original.rstrip() + "\n\n" + notes).strip(), True


def validate_notes(description, start, end, project_url, release_version):
    notes = approved_notes(description, release_version)
    items = entries(start, end, project_url)
    for item in items:
        marker = f"[{item['key']}]({item['link']})"
        require(notes.count(marker) == 1, f"Release notes must link {item['key']} exactly once")
        heading = CATEGORIES[item["category"]]
        parts = notes.split(f"### {heading}", 1)
        require(len(parts) == 2 and marker in parts[1].split("\n### ", 1)[0],
                f"Release notes put {item['key']} in the wrong category")
        if item["category"] == "breaking":
            after = parts[1].split(marker, 1)[1].split("\n- ", 1)[0].split("\n### ", 1)[0]
            impact = re.search(r"(?m)^  Compatibility: (.+)$", after)
            migration = re.search(r"(?m)^  Migration: (.+)$", after)
            require(impact and migration and
                    not impact.group(1).startswith("None") and
                    not migration.group(1).startswith("None"),
                    f"Breaking change {item['key']} needs compatibility and migration text")
    source_links = re.findall(re.escape(project_url.rstrip("/")) + r"/-/merge_requests/\d+", notes)
    require(len(source_links) == len(items), "Release notes have extra or duplicate MR links")
    require(items or "No changes." in notes, "Empty release notes need 'No changes.'")
    return notes


def api_request(method, api_url, project_id, endpoint, token, body=None, private=False):
    require(api_url and project_id and token, "GitLab API URL, project ID and token are required")
    url = f"{api_url.rstrip('/')}/projects/{urllib.parse.quote(str(project_id), safe='')}{endpoint}"
    headers = {"Content-Type": "application/json"}
    name = "PRIVATE-TOKEN" if private else "JOB-TOKEN"
    headers[name] = token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ReleaseError(f"GitLab API {method} {endpoint}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"GitLab API {method} {endpoint}: {exc.reason}") from exc


def mr_description(iid, require_live=False):
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_MERGE_REQUEST_PROJECT_ID") or os.environ.get("CI_PROJECT_ID")
    token = os.environ.get("GITLAB_ACCESS_TOKEN") or os.environ.get("CI_JOB_TOKEN")
    if api_url and project_id and iid and token:
        try:
            mr = api_request("GET", api_url, project_id, f"/merge_requests/{iid}", token,
                             private=bool(os.environ.get("GITLAB_ACCESS_TOKEN")))
            return mr.get("description") or ""
        except ReleaseError:
            if require_live or not os.environ.get("CI_MERGE_REQUEST_DESCRIPTION"):
                raise
    require(not require_live, "Release MR validation needs GitLab API read access")
    require(os.environ.get("CI_MERGE_REQUEST_DESCRIPTION_IS_TRUNCATED") != "true",
            "MR description is truncated; configure GitLab API read access")
    return os.environ.get("CI_MERGE_REQUEST_DESCRIPTION", "")


def tag_check(tag, main, project_url):
    require(VERSION_RE.fullmatch(tag), f"Tag {tag!r} must be vX.Y.Z")
    require(git("cat-file", "-t", "refs/tags/" + tag) == "tag", "Release tag must be annotated")
    tagged = git("rev-parse", tag + "^{commit}")
    require(tagged == git("rev-parse", main), f"Tag {tag} must point to current {main}")
    parents = git("rev-list", "--parents", "-n", "1", tagged).split()
    require(len(parents) == 3, "Release tag must point to a development-to-master merge commit")
    require(git("show", "-s", "--format=%s", tagged) == "Merge branch 'development' into 'master'",
            "Release tag must point to the development-to-master release merge")
    require(tag == "v" + validate_versions(), "Tag and VERSION differ")
    _, start = previous_release(main, before=tag)
    description = git("show", "-s", "--format=%B", tagged)
    return validate_notes(description, start, parents[2], project_url, version())


def publish(tag, main, project_url):
    notes = tag_check(tag, main, project_url)
    api_url = os.environ.get("CI_API_V4_URL")
    project_id = os.environ.get("CI_PROJECT_ID")
    token = os.environ.get("RELEASE_ACCESS_TOKEN") or os.environ.get("CI_JOB_TOKEN")
    # No binary asset: GitLab attaches the source archives automatically.
    body = {"name": f"Release {tag}", "tag_name": tag, "description": notes}
    api_request("POST", api_url, project_id, "/releases", token, body,
                private=bool(os.environ.get("RELEASE_ACCESS_TOKEN")))
    print(f"Created GitLab Release {tag}")


def private_files(revision):
    """Return all GVL paths in a commit or tree."""
    names = git("ls-tree", "-r", "-z", "--name-only", revision).split("\0")
    return [name for name in names if PRIVATE_FILE_RE.search(name)]


def public_tree(revision):
    """Return the tree of revision without PUBLIC_EXCLUDE; fail on leftover GVLs."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", revision + "^{tree}", env=env)
        excluded = [name for name in git("ls-files", "-z", "--", *PUBLIC_EXCLUDE, env=env).split("\0")
                    if name]
        if excluded:
            git("update-index", "--force-remove", "--", *excluded, env=env)
        tree = git("write-tree", env=env)
    leaked = private_files(tree)
    require(not leaked, "Public snapshot would contain GVLs; move them into an excluded folder "
                        f"or extend PUBLIC_EXCLUDE: {', '.join(leaked)}")
    return tree


def public_commit(tag, base):
    """Build the public commit for tag on top of base, without internal parents.

    Author, committer and date come from the annotated tag, so rebuilding the
    same tag on the same base always gives the same SHA.
    """
    tree = public_tree(tag)
    tagger = git("for-each-ref", "--format=%(taggername)%00%(taggeremail)%00%(taggerdate:raw)",
                 "refs/tags/" + tag).split("\0")
    require(len(tagger) == 3 and tagger[0], f"Tag {tag} needs a tagger (annotated tag)")
    name, email, date = tagger[0], tagger[1].strip("<>"), tagger[2]
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}
    parents = ["-p", base] if base != ZERO else []
    return git("commit-tree", tree, *parents, "-m", f"Release {tag}", env=env)


def github_head(repo, branch):
    """Fetch the public branch from GitHub (read-only) and return its SHA."""
    url = f"https://github.com/{repo}.git"
    line = git("ls-remote", url, "refs/heads/" + branch)
    if not line:
        return ZERO
    git("fetch", "--quiet", "--no-tags", url, "refs/heads/" + branch)
    return line.split()[0]


def github_snapshot(tag, main, project_url, repo, branch, output):
    tag_check(tag, main, project_url)
    base = github_head(repo, branch)
    commit = public_commit(tag, base)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "public-commit.txt").write_text(commit + "\n", encoding="utf-8")
    (out / "public-files.txt").write_text(git("ls-tree", "-r", "--name-only", commit) + "\n",
                                          encoding="utf-8")
    if base == ZERO:
        diff = git("show", "--stat", "--patch", "--format=", commit)
        summary = "(first public commit)"
    else:
        diff = git("diff", "--stat", "--patch", base, commit)
        summary = git("diff", "--stat", base, commit)
    (out / "public-changes.diff").write_text(diff + "\n", encoding="utf-8")
    print(f"Public commit {commit} for {tag} on {repo}:{branch} (parent {base[:12]})")
    print(summary)
    print("Review the artifacts, then start github_publish manually.")


def github_request(method, repo, endpoint, token, body):
    url = f"https://api.github.com/repos/{repo}{endpoint}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "eta-release-ci",
               "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ReleaseError(f"GitHub API {method} {endpoint}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"GitHub API {method} {endpoint}: {exc.reason}") from exc


def github_publish(tag, main, project_url, repo, branch, reviewed_file):
    token = os.environ.get("GITHUB_TOKEN")
    require(token, "Set GITHUB_TOKEN (contents: write on the GitHub repository)")
    notes = tag_check(tag, main, project_url)
    reviewed = Path(reviewed_file).read_text(encoding="utf-8").strip()
    base = github_head(repo, branch)
    commit = public_commit(tag, base)
    require(commit == reviewed,
            f"Public commit {commit[:12]} differs from reviewed {reviewed[:12]} "
            "(GitHub branch changed?). Rerun github_snapshot and review again.")
    # Token only via environment git config, so it never shows up in arguments or errors.
    auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}"}
    git("push", f"https://github.com/{repo}.git", f"{commit}:refs/heads/{branch}", env=env)
    print(f"Pushed {commit} to {repo}:{branch}")
    release = github_request("POST", repo, "/releases", token,
                             {"tag_name": tag, "target_commitish": commit, "name": f"Release {tag}",
                              "body": notes, "draft": True})
    print(f"Created draft GitHub release: {release.get('html_url', tag)}")
    print("Check the text, then publish it on GitHub (this triggers Zenodo).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-mr")
    previous = sub.add_parser("previous-start")
    previous.add_argument("--main", default="origin/master")
    for command in ("draft", "validate-release"):
        p = sub.add_parser(command)
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        p.add_argument("--project-url", required=True)
        p.add_argument("--version", required=True)
        if command == "draft":
            p.add_argument("--output", required=True)
    update = sub.add_parser("update-mr")
    update.add_argument("--notes-file", required=True)
    update.add_argument("--api-url", required=True)
    update.add_argument("--project-id", required=True)
    update.add_argument("--mr-iid", required=True)
    for command in ("tag-check", "publish"):
        p = sub.add_parser(command)
        p.add_argument("--tag", required=True)
        p.add_argument("--main", default="origin/master")
        p.add_argument("--project-url", required=True)
    check = sub.add_parser("public-check")
    check.add_argument("--revision", required=True)
    for command in ("github-snapshot", "github-publish"):
        p = sub.add_parser(command)
        p.add_argument("--tag", required=True)
        p.add_argument("--main", default="origin/master")
        p.add_argument("--project-url", required=True)
        p.add_argument("--repo", required=True)
        p.add_argument("--branch", required=True)
        if command == "github-snapshot":
            p.add_argument("--output", required=True)
        else:
            p.add_argument("--reviewed", required=True)
    args = parser.parse_args()
    if args.command == "validate-mr":
        parse_changes(mr_description(os.environ.get("CI_MERGE_REQUEST_IID")))
        print("MR release-note entries are valid")
    elif args.command == "previous-start":
        print(previous_release(args.main)[1])
    elif args.command == "draft":
        require(args.version == version(), "Requested release version differs from VERSION")
        Path(args.output).write_text(draft(args.start, args.end, args.project_url, args.version),
                                     encoding="utf-8")
        print(f"Wrote draft to {args.output}")
    elif args.command == "validate-release":
        require(args.version == version(), "Requested release version differs from VERSION")
        require_new_version("origin/master")
        require(os.environ.get("CI_MERGE_REQUEST_SQUASH_ON_MERGE") != "true",
                "Release MR must have squash disabled")
        validate_versions()
        validate_notes(mr_description(os.environ.get("CI_MERGE_REQUEST_IID"), require_live=True), args.start,
                       args.end, args.project_url, args.version)
        print("Approved MR notes cover the release changes")
    elif args.command == "update-mr":
        token = os.environ.get("GITLAB_ACCESS_TOKEN")
        require(token, "Set GITLAB_ACCESS_TOKEN to a project/group access token")
        notes = Path(args.notes_file).read_text(encoding="utf-8")
        endpoint = f"/merge_requests/{args.mr_iid}"
        mr = api_request("GET", args.api_url, args.project_id, endpoint, token, private=True)
        original = mr.get("description") or ""
        description, changed = insert_notes(original, notes)
        if not changed:
            print("MR notes already match; no update needed")
        else:
            api_request("PUT", args.api_url, args.project_id, endpoint, token,
                        {"description": description}, private=True)
            print(f"Added release notes to MR !{args.mr_iid}")
    elif args.command == "tag-check":
        tag_check(args.tag, args.main, args.project_url)
        print(f"Annotated tag {args.tag} and approved release notes are valid")
    elif args.command == "publish":
        publish(args.tag, args.main, args.project_url)
    elif args.command == "public-check":
        public_tree(args.revision)
        print("Public snapshot would contain no GVLs")
    elif args.command == "github-snapshot":
        github_snapshot(args.tag, args.main, args.project_url, args.repo, args.branch, args.output)
    else:
        github_publish(args.tag, args.main, args.project_url, args.repo, args.branch, args.reviewed)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        sys.exit(1)
