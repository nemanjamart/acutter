import json
import os
import pprint
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from functools import wraps

import click
import pkg_resources
import slugify
import toml
from cookiecutter.main import cookiecutter
from toml import TomlArraySeparatorEncoder, TomlEncoder

TEMPLATEDIR = os.path.join(
    os.path.abspath(os.path.dirname(__file__) + "/.."), "templates"
)


def get_templatedir(template):
    templatedir = os.path.join(TEMPLATEDIR, template)
    if not os.path.exists(templatedir):
        raise Exception(
            "Cookiecutter template={} not found inside templatedir={}".format(
                template, templatedir
            )
        )
    return templatedir


def inprojhome(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not os.path.exists("./pyproject.toml"):
            raise Exception(
                "This command has to be executed in the root directory of a project"
            )
        f(*args, **kwargs)

    return wrapper


@click.group()
def cli():
    pass


@inprojhome
@cli.command()
def docs():
    """Re-Generate documentation"""
    subprocess.check_call(["sphinx-build", "docs", ".docs"])


@cli.command()
@click.argument("folder", type=click.Path())
@click.option("--force", default=False, help="force creation even if the folder exists")
@click.option("--template", default="python_package", help="Project template to use")
def create(folder, force, template):
    """
    Create a new project inside a new folder (project name will be the basedir)
    """

    if os.path.exists(folder):
        if not force:
            raise Exception("The {} already exists".format(folder))

    outdir = os.path.dirname(os.path.abspath(folder))
    context = {
        "initial_commit": "y",
        "setup_github": "n",
        "setup_pre_commit": "y",
        "private_or_public": "public",
        "run_virtualenv_install": "y",
        "project_name": os.path.basename(folder),
    }
    cookiecutter(
        get_templatedir(template),
        no_input=False,
        extra_context=context,
        overwrite_if_exists=force,
        output_dir=outdir,
    )


@cli.command()
@click.argument("folder", type=click.Path(exists=True))
@click.option("--template", default="python_package", help="Project template to use")
def provision(folder, template):
    """
    Generate pyproject.toml for a repository which doesn't have it.
    After this command was successful; you can run 'update'
    """

    inputfile = os.path.join(folder, "pyproject.toml")
    if os.path.exists(inputfile):
        raise Exception(
            "This repo does have pyproject.toml file. Perhaps try 'update' command?"
            "Or delete {}".format(inputfile)
        )

    print(
        "First, we'll generate new cookiecutter - please answer these questions"
        "(do not worry, original repository will be unchanged"
    )
    # first run cookiecutter
    templatedir = get_templatedir(template)
    tmpdir = tempfile.mkdtemp()
    context = {
        "initial_commit": "n",
        "setup_github": "n",
        "setup_pre_commit": "n",
        "private_or_public": "private",
        "run_virtualenv_install": "n",
        "project_name": os.path.basename(folder),
    }
    result = cookiecutter(
        templatedir,
        no_input=False,
        extra_context=context,
        overwrite_if_exists=True,
        output_dir=tmpdir,
    )

    # now grab the generated pyproject.toml and copy it
    newtoml = os.path.join(result, "pyproject.toml")

    if os.path.exists(newtoml):
        shutil.copyfile(newtoml, inputfile)
        print("New config written into: {}".format(inputfile))
    else:
        print("Process interrupted; no configuration generated")
        print(result)


@cli.command()
@click.argument("folder", type=click.Path(exists=True))
@click.option("--dry-run", default=False)
@click.option("--template", default="python_package", help="Project template to use")
@click.option(
    "--force",
    default=False,
    is_flag=True,
    help="Force update based on a different template",
)
def update(folder, dry_run, template, force):
    """Update repository which contains pyproject.toml

    When given path pointing to a repository (that was previously) created
    using our cookie cutter template, it will read info off that repo and
    regenerate the project; effectively updating the files.

    CAREFUL: you must manually review the changes and revert those that
    should not be accepted: i.e. use `git checkout -- <path>` to get them back

    """

    inputfile = check_pyproject(folder)

    # find out on what template this project has been based
    tomldata = toml.load(inputfile)
    try:
        ptemplate = tomldata["tool"]["acutter"]["template"]
    except KeyError:
        ptemplate = template

    if ptemplate == "":
        raise Exception(
            "Please tell me what template to base this project on. tool.acutter doesn't contain that info"
        )

    if ptemplate != template:
        if force:
            ptemplate = template
        else:
            raise Exception(
                "The project template differs from your argument; use --force to override. project={}, passed={}".format(
                    ptemplate, template
                )
            )

    templatedir = get_templatedir(ptemplate)
    context = get_project_context(inputfile, templatedir)
    output_dir = os.path.abspath(os.path.join(folder, ".."))

    basename = os.path.basename(os.path.abspath(folder))
    if context["project_name"] != basename:
        print(
            "project_name differs from the location on disk; will use location: {} -> {}".format(
                context["project_name"], basename
            )
        )
        context["project_name"] = basename

    if not dry_run:
        oldtoml = toml.load(inputfile, _dict=OrderedDict)
        cookiecutter(
            templatedir,
            no_input=True,
            extra_context=context,
            overwrite_if_exists=True,
            output_dir=output_dir,
        )
        merge_old_new(oldtoml, inputfile)
    else:
        print("Would have called cookiecutter with:")
        pprint.pprint(
            dict(
                templatedir=templatedir,
                no_input=True,
                extra_context=context,
                overwrite_if_exists=True,
                output_dir=output_dir,
            )
        )


@cli.command()
@click.argument("folder", type=click.Path(exists=True))
@click.option(
    "--force",
    default=False,
    is_flag=True,
    help="Will continue even if .env is detected",
)
def setup_virtualenv(folder, force):
    """
    Helper function you can call to setup python virtualenv for the project
    It will do the following:

        - create virtualenv inside <project>/.venv
        - install project dependencies (incl .[dev] and .[docs])
        - install pre-commit hooks
    """

    check_pyproject(folder)
    venv = os.path.join(os.path.abspath(folder), ".venv")
    if os.path.exists(venv) and not force:
        raise Exception("{} already exists, use --force to continue".format(venv))
    install_virtualenv(folder)
    setup_pre_commit(folder)


# -------------------------------------------------------------------------


def run_cmd(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def run_pip(args, cwd=None):
    cmd = [".venv/bin/python", "-m", "pip"]
    cmd += args
    run_cmd(cmd, cwd=cwd)


def check_command_exists(cmd, cwd=None):
    try:
        run_cmd([cmd, "-h"], capture_output=True, cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"{cmd} command is not installed")
        return False
    return True


def install_virtualenv(cwd):
    if not check_command_exists("virtualenv"):
        return

    run_cmd(["virtualenv", ".venv"], cwd=cwd)
    run_pip(["install", ".[dev]"], cwd=cwd)
    run_pip(["install", ".[docs]"], cwd=cwd)
    run_pip(["install", "-e", "."], cwd=cwd)  # should be last to get proper scripts

    print(
        """Virtualenv created inside {folder}/.venv

    In case of problems, you can run (manually):

    cd {folder}
    source .venv/bin/activate
    pip install .
    pip install -e .[dev]
    pip install .[docs]
    """.format(
            folder=os.path.abspath(cwd)
        )
    )


def setup_pre_commit(cwd):
    if check_command_exists(".venv/bin/pre-commit", cwd=cwd):
        # Run pre-commit install
        run_cmd([".venv/bin/pre-commit", "install"])
        run_cmd([".venv/bin/pre-commit", "install", "--hook-type", "commit-msg"])

    elif check_command_exists("pre-commit", cwd=cwd):
        # Run pre-commit install
        run_cmd(["pre-commit", "install"])
        run_cmd(["pre-commit", "install", "--hook-type", "commit-msg"])


def check_pyproject(folder):
    inputfile = os.path.join(folder, "pyproject.toml")
    if not os.path.exists(inputfile):
        raise Exception(
            "This repo doesnt have pyproject.toml file. Perhaps try to run 'provision' command first?"
        )
    return inputfile


# ---------------------------------------------------------------------------------


def merge_old_new(oldtoml, inputfile):
    """
    Newly generated toml has overwritten things that we want to preserve
    (such as dependencies); in this function we try to put back the old
    and keep the new
    """

    updated = 0
    newtoml = toml.load(inputfile, _dict=OrderedDict)
    for getter in (
        lambda x: x["project"]["dependencies"],
        lambda x: x["project"]["optional-dependencies"]["dev"],
        lambda x: x["project"]["optional-dependencies"]["docs"],
    ):
        try:
            old = list(pkg_resources.parse_requirements(getter(oldtoml)))
            new = list(pkg_resources.parse_requirements(getter(newtoml)))

            newkeys = set([x.name for x in new])
            oldkeys = set([x.name for x in old])

            # if a package is present in the template but missing from the
            # 'old' pyproject.toml we assume it was removed by a developer
            to_keep = []
            for dep in new:
                if dep.name in oldkeys:
                    to_keep.append(str(dep))

            if len(to_keep) != len(new):
                getter(newtoml).clear()
                getter(newtoml).extend(to_keep)
                updated += len(to_keep)

            for dep in old:
                if dep.name not in newkeys:
                    getter(newtoml).append(str(dep))
                    updated += 1
        except KeyError:
            pass

    # keep values from the original
    for getter in (
        lambda x: x["xsetup"]["entry_points"]["console_scripts"],
        lambda x: x["xsetup"]["console_scripts"],
    ):
        try:
            old = getter(oldtoml)
            new = getter(newtoml)
            if str(old) != str(new):
                new.clear()
                new.extend(old)
        except KeyError:
            pass

    # only if changed, to preserve potential comments otherwise
    if updated > 0:
        with open(inputfile, "w") as fo:
            fo.write(dumps(newtoml, CustomEncoder()))


def get_project_context(inputfile, templatedir, template="cookiecutter.json"):

    # <project>/pyproject.toml
    tomldata = toml.load(inputfile)

    # cookiecutter json stuff
    with open(os.path.join(templatedir, template), "r") as fi:
        jdata = json.load(fi)

    project = tomldata["project"]
    print("Settings loaded from: {}\n".format(inputfile))
    pprint.pprint(project)
    print("-" * 80)

    print("Current cookicutter template defaults:\n")
    pprint.pprint(jdata)
    print("-" * 80)

    # those things should not be changed (in existing repository)
    out = {
        "initial_commit": "n",
        "setup_github": "n",
        "setup_pre_commit": "n",
        "private_or_public": "private",
        "run_virtualenv_install": "n",
    }

    # take the first entry name
    out["email"] = project.get("authors", [{"email": None}])[0].get(
        "email", jdata["email"]
    )
    out["full_name"] = project.get("authors", [{"name": None}])[0].get(
        "name", jdata["full_name"]
    )
    repo = project.get("repository", None)
    if repo:
        parts = repo.rsplit("/", 2)
        out["github_username"] = parts[1]
        out["project_name"] = parts[2].replace(".git", "")
        # old version of packages may read {include = []}
        pkgs = project.get("packages", [slugify.slugify(out["project_name"])])
        if isinstance(pkgs[0], dict):
            pkgs = [x["include"] for x in pkgs]
        out["package_name"] = pkgs[0]
        out["project_slug"] = out["package_name"]

    out["open_source_license"] = project.get(
        "license", {"text": "Not open source"}
    ).get("text")
    out["version"] = project.get("version", jdata["version"])
    out["project_short_description"] = project.get(
        "description", jdata["project_short_description"]
    )

    print("And this is what we'll use:\n")
    pprint.pprint(out)
    print("-" * 80)
    return out


class CustomEncoder(TomlArraySeparatorEncoder):
    def __init__(self, _dict=OrderedDict, preserve=True, separator=",\n"):
        super(CustomEncoder, self).__init__(_dict, preserve)
        if separator.strip() == "":
            separator = "," + separator
        elif separator.strip(" \t\n\r,"):
            raise ValueError("Invalid separator for arrays")
        self.separator = separator

    def dump_list(self, v):
        t = []
        retval = "[\n"
        for u in v:
            t.append(self.dump_value(u))
        while t != []:
            s = []
            for u in t:
                if isinstance(u, list):
                    for r in u:
                        s.append(r)
                else:
                    retval += "    " + str(u) + self.separator
            t = s
        retval += "]\n"
        return retval


def dumps(o, encoder=None, prefix=""):
    """Modified version of toml.dumps()
    https://github.com/uiri/toml/blob/59d83d0d51a976f11a74991fa7d220fc630d8bae/toml/encoder.py#L34

    In here we dump the sections in order; the original logic of the toml.dumps()
    is rather convoluted - instead of recursively build the sections, from bottom up,
    it collects sections and those that are new, are processed last. Our version
    may not be the best either - but we'll dump sections on the first encounter
    """

    retval = ""
    if encoder is None:
        encoder = TomlEncoder(o.__class__)
    addtoretval, sections = encoder.dump_sections(o, "")
    if prefix and addtoretval:
        retval += "[{}]\n{}".format(prefix, addtoretval)
    else:
        retval += addtoretval
    outer_objs = [id(o)]

    section_ids = [id(section) for section in sections.values()]
    for outer_obj in outer_objs:
        if outer_obj in section_ids:
            raise ValueError("Circular reference detected")
    outer_objs += section_ids

    for section in sections:
        addtoretval, addtosections = encoder.dump_sections(sections[section], section)

        if addtoretval or (not addtoretval and not addtosections):
            if retval and retval[-2:] != "\n\n":
                retval += "\n"
            retval += "[" + (prefix and prefix + "." or "") + section + "]\n"
            if addtoretval:
                retval += addtoretval
        for s in addtosections:
            if prefix:
                p = prefix + "." + section + "." + s
            else:
                p = section + "." + s
            retval += dumps(addtosections[s], encoder=encoder, prefix=p)

    return retval


if __name__ == "__main__":
    cli()
