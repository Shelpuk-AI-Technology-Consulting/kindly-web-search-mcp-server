#!/usr/bin/env bash
# CI preflight: prove the capabilities a job invokes are present, BEFORE it uses them.
#
# Adopted verbatim from an internal repository where it is in production. Only this header
# differs, and it differs because two of the sentences it used to carry are NOT true
# here -- see the guard note below. Everything from `set -uo pipefail` down is
# byte-identical on purpose, so a fix made upstream can be carried across by copying
# the body.
#
# Nothing in a runner label tells you which TOOLS the machine carries, and this is
# what checks that at run time, before the step that needs them.
#
# 🔴 Why this matters more than its size suggests. Upstream measured `unzip` present
# on one runner and absent on another one day apart, across a heterogeneous
# self-hosted fleet. A missing tool therefore does not fail a job -- it fails it only
# when placement is unlucky, and the re-run passes. That is the most expensive failure
# class a shared fleet has: one upstream incident cost five days of undeployable
# frontends before anyone identified a missing Docker daemon as the cause.
#
# ⚠️ **That measurement is INHERITED and describes a fleet this repository does not
# use.** This repository's workflows run on a GitHub-hosted runner, which is uniform
# per label rather than heterogeneous -- so the "unlucky placement" shape does not
# apply here.
#
# 🔴 **The risk it is kept for is different and just as real: a SLIM IMAGE.** Being
# slim is precisely a claim to carry fewer packages than the full image, and `unzip`
# -- which `anthropics/claude-code-action` shells out to, two levels down, without
# ever naming it in this repository -- is exactly what gets trimmed. Uniform means
# every run fails the same way rather than one in three, which is easier to diagnose
# and no less broken.
#
# ⚠️ On a GitHub-hosted runner this should SELF-HEAL rather than fail: the job has
# passwordless sudo, so a missing package is installed and a warning emitted. Read
# that warning as a finding about the image -- it costs every run the download.
#
# 🔴 **THE OTHER HALF OF THE UPSTREAM GUARANTEE IS ABSENT HERE, AND THAT IS THE THING
# TO KNOW.** Upstream pairs this with two `lint`-job guards -- one proving a job asks
# for a runner label that exists, and one proving every job that needs a capability
# actually calls this script. This repository has neither, so `claude-code-review.yml`
# calling this is a CONVENTION rather than an enforced invariant: a future job that
# needs `unzip` and forgets the call fails the way this script exists to prevent, and
# nothing goes red to say so.
#
# ⚠️ `test_the_review_job_names_a_known_runner_as_a_literal` covers a slice of the
# first guard -- it refuses a label outside a written-down set -- but it cannot tell
# whether that label is actually OFFERED to this repository, which is the half that
# queues for ever in silence. Port the real guards the day a third workflow lands.
#
# ⚠️ **Shell, not Python, and that is a claim about this script's LANGUAGE, not its
# position.** A job may need a capability BEFORE it provisions an interpreter -- and
# reaching for the system interpreter is not an option: it has no pip, and a stale
# broken tool-cache interpreter can precede `setup-python`'s on `PATH`. A Python
# preflight could therefore only run after provisioning, which is after several of the
# steps it exists to protect.
#
# ⚠️ **Where this step goes is a separate rule: before the first step that USES the
# capability.** In `claude-code-review.yml` that is before `anthropics/claude-code-action`,
# which is what shells out to `unzip`.
#
# Usage:
#     .github/review/scripts/ci_preflight.sh docker
#     .github/review/scripts/ci_preflight.sh jq unzip
#     .github/review/scripts/ci_preflight.sh --self-test
#
# Exits 0 when every named capability is usable from the calling step, 1 otherwise.

set -uo pipefail

# 🔴 `docker info`, never `command -v docker`. upstream was a missing DAEMON, and a client with
# no reachable daemon passes a `command -v` and then dies at the first build -- the exact
# symptom-shaped failure this exists to convert into a named one. `info` talks to the daemon;
# `--version` does not.
#
# ⚠️ Capabilities that CANNOT be installed by a job carry an empty package on purpose. Docker
# is a daemon, a socket and a group membership on a shared machine; a step that pretended to
# install it would appear to self-heal and would not. It fails, naming the runner.
#
# The pattern for the installable ones is `claude-code-review.yml`'s, which README.md calls
# "the pattern worth copying": probe, install if the box allows it, otherwise fail naming the
# package rather than 20 lines into a third-party action's log.
CI_PREFLIGHT_KNOWN="docker jq unzip"

# `never` forces the fail-closed branch. The self-test sets it; CI never does. It can only make
# this script STRICTER, which is the safe direction for an override to fail in.
CI_PREFLIGHT_INSTALL="${CI_PREFLIGHT_INSTALL:-auto}"

capability_package() {
    # Return the apt package that supplies a capability, or nothing when none can.
    case "$1" in
        jq) printf 'jq' ;;
        unzip) printf 'unzip' ;;
        docker) printf '' ;;
        *) printf '' ;;
    esac
}

capability_present() {
    # Report whether a capability is usable from THIS step's shell, right now.
    case "$1" in
        docker) docker info >/dev/null 2>&1 ;;
        jq) command -v jq >/dev/null 2>&1 ;;
        unzip) command -v unzip >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

#: Why the last install attempt did not happen, or did not work. Set by `install_capability`,
#: read by `capability_why`.
#:
#: 🔴 **One message for four causes is a wrong-cause message three times out of four**, and in
#: the artefact whose whole purpose is turning symptom-shaped failures into named ones that is
#: not a nitpick. `install_capability` can fail because nothing packages the capability, because
#: installs are disabled, because the job is not root and has no passwordless sudo, or because
#: `apt-get` itself failed -- an unreachable mirror or a moved repository. The remedy is the
#: same in all four ("add it to the runner image"), which is exactly why the wrong cause was
#: cheap to ship and expensive to read: a reader sent to check `sudo` when the mirror was down
#: spends their time on the wrong machine.
CI_PREFLIGHT_INSTALL_REASON=""

capability_why() {
    # Return the one-line remedy printed with a failure. It names the ACTION a reader can take,
    # because "docker is missing" without "and a job cannot install one" sends the next person
    # to add an install step that cannot work.
    case "$1" in
        docker)
            printf '%s' "\`docker info\` failed: either the client is absent or the daemon is \
unreachable from this step. A job cannot install a daemon on a shared machine -- add Docker to \
the runner image, or take this machine out of the pool."
            ;;
        *)
            printf '%s' "${CI_PREFLIGHT_INSTALL_REASON:-no install was attempted}. Add the \
package to the runner image."
            ;;
    esac
}

install_capability() {
    # Try to install a capability's package, and report whether it can even be attempted.
    #
    # ⚠️ The `sudo -n` branch is written as a BRANCH, not an assumption, exactly as README.md
    # instructs: passwordless sudo "fails everywhere" is an observation about some machines and
    # has never been probed per machine on this fleet.
    local package
    package="$(capability_package "$1")"
    # 🔴 CLEARED on entry. `ensure_capability` is called once per capability in a job that may
    # name several, so a reason left over from an earlier one would be printed against a later
    # one -- a wrong cause built out of a right one.
    CI_PREFLIGHT_INSTALL_REASON=""
    if [ -z "$package" ]; then
        CI_PREFLIGHT_INSTALL_REASON="no package supplies it and a job cannot install one"
        return 1
    fi
    if [ "$CI_PREFLIGHT_INSTALL" != "auto" ]; then
        CI_PREFLIGHT_INSTALL_REASON="installs are disabled here \
(CI_PREFLIGHT_INSTALL=$CI_PREFLIGHT_INSTALL)"
        return 1
    fi
    if [ "$(id -u)" = "0" ]; then
        apt-get update -qq && apt-get install -y -qq "$package" && return 0
        CI_PREFLIGHT_INSTALL_REASON="this job IS root and \`apt-get\` still failed for \
\`$package\` -- the mirror is unreachable from this runner, or the repository moved"
        return 1
    fi
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$package" && return 0
        CI_PREFLIGHT_INSTALL_REASON="passwordless \`sudo\` works here and \`apt-get\` still \
failed for \`$package\` -- the mirror is unreachable from this runner, or the repository moved"
        return 1
    fi
    CI_PREFLIGHT_INSTALL_REASON="this job is not root and has no passwordless sudo, so it \
cannot install \`$package\`"
    return 1
}

ensure_capability() {
    # Prove one capability is usable, installing it when the runner permits. Returns non-zero
    # with an `::error::` annotation naming the capability, the runner and the job otherwise.
    local capability="$1"
    local runner="${RUNNER_NAME:-this runner}"
    local job="${GITHUB_JOB:-this job}"

    case " $CI_PREFLIGHT_KNOWN " in
        *" $capability "*) ;;
        *)
            # 🔴 An unknown name is a FAILURE, not a skip. A typo that reported "ok" would be a
            # green check over a capability nothing ever probed -- and the guard would have
            # accepted it too, since it refuses arguments it does not know.
            echo "::error::preflight: \`$capability\` is not a known capability (known: \
$CI_PREFLIGHT_KNOWN). Teach scripts/ci_preflight.sh and scripts/check_job_preflight.py about \
it before a job asks for it."
            return 1
            ;;
    esac

    if capability_present "$capability"; then
        echo "preflight ok  $capability  on $runner"
        return 0
    fi

    if install_capability "$capability" >/dev/null 2>&1; then
        if capability_present "$capability"; then
            echo "::warning::preflight: $capability was missing from $runner and this job \
installed it. Add it to the runner image -- an install here costs every run on that machine."
            return 0
        fi
        # ⚠️ The install reported SUCCESS and the capability is still not usable. Without this
        # branch the message below falls back to whatever reason the last FAILING attempt left
        # -- or to "no install was attempted", which is the opposite of what happened.
        CI_PREFLIGHT_INSTALL_REASON="installing \`$(capability_package "$capability")\` \
reported SUCCESS and $capability is still not usable -- it may have landed outside this step's \
PATH, or its post-install failed quietly"
    fi

    echo "::error::preflight: \`$capability\` is NOT available on runner '$runner' (job \
'$job'): $(capability_why "$capability")"
    return 1
}

main() {
    if [ "$#" -eq 0 ]; then
        echo "::error::preflight: no capability named. Usage: scripts/ci_preflight.sh \
<capability>... (known: $CI_PREFLIGHT_KNOWN)"
        return 1
    fi

    # Every capability is checked, not the first failing one: a job missing two tools should
    # need one run to learn both. `missing` is counted rather than short-circuited for that.
    local missing=0
    for capability in "$@"; do
        ensure_capability "$capability" || missing=$((missing + 1))
    done
    return $((missing > 0))
}

self_test() {
    # Drive every verdict against a synthetic PATH, so this proves the DISPATCH and the
    # VERDICT with nothing installed and nothing changed on the machine.
    #
    # 🔴 What it does NOT prove, said plainly so a green one is not read as evidence about the
    # fleet: it cannot show a real runner's answer. Only the preflight steps in the real jobs
    # do that, which is why this is wired into the jobs and not only into `lint`.
    local root bin failures=0
    # 🔴 Resolved BEFORE `PATH` is replaced. The synthetic `PATH` below is a single directory,
    # which takes `chmod`, `rm` and `mkdir` away from the harness along with `jq` and `docker`.
    # That is not a cosmetic breakage: a stub that failed to become executable reads as
    # "capability absent", so a case expecting absence PASSES having proved nothing.
    local chmod_bin rm_bin
    chmod_bin="$(command -v chmod)"
    rm_bin="$(command -v rm)"
    root="$(mktemp -d)"
    bin="$root/bin"
    mkdir -p "$bin"
    trap '"$rm_bin" -rf "$root"' RETURN

    local saved_path="$PATH"
    local saved_install="$CI_PREFLIGHT_INSTALL"

    _case() {
        # Run one case: label, expected exit status, then the command.
        local label="$1" expected="$2"
        shift 2
        local output status
        output="$("$@" 2>&1)"
        status=$?
        if [ "$status" = "$expected" ]; then
            echo "  [PASS] $label"
        else
            echo "  [FAIL] $label -- expected exit $expected, got $status"
            echo "         output: $output"
            failures=$((failures + 1))
        fi
    }

    _says() {
        # Run one case that asserts on the MESSAGE, not only the exit status: label, a substring
        # the output must contain, then the command.
        #
        # 🔴 Without this, every install-failure case is satisfied by "it exited 1" -- and the
        # defect a review found here was a failure that exited 1 with the WRONG CAUSE named.
        # An exit-status assertion cannot see that, which is why these cases exist.
        local label="$1" want="$2"
        shift 2
        local output
        output="$("$@" 2>&1)"
        case "$output" in
            *"$want"*) echo "  [PASS] $label" ;;
            *)
                echo "  [FAIL] $label -- output did not contain: $want"
                echo "         output: $output"
                failures=$((failures + 1))
                ;;
        esac
    }

    _stub() {
        # Write an executable stub onto the synthetic PATH. `$2` is its exit status.
        printf '#!/bin/sh\nexit %s\n' "$2" > "$bin/$1"
        "$chmod_bin" +x "$bin/$1"
        # A stub that is not executable is indistinguishable from an absent one, and the
        # absence cases would then pass without a stub having been written at all.
        [ -x "$bin/$1" ] || { echo "  [FAIL] stub $1 is not executable"; failures=$((failures + 1)); }
    }

    # An EMPTY PATH, so nothing on the real machine can make a case pass by accident -- which
    # would be the vacuous direction here: every "present" case passing because the host has
    # the tool, on a script whose whole job is to notice that it does not.
    PATH="$bin"
    CI_PREFLIGHT_INSTALL=never

    echo "ci_preflight.sh --self-test"

    _case "an unknown capability is refused, not skipped" 1 ensure_capability nosuchtool
    _case "no capability named at all is refused" 1 main

    _case "jq absent and uninstallable fails" 1 ensure_capability jq
    _stub jq 0
    _case "jq present passes" 0 ensure_capability jq

    _case "unzip absent and uninstallable fails" 1 ensure_capability unzip
    _stub unzip 0
    _case "unzip present passes" 0 ensure_capability unzip

    # 🔴 THE `docker info` CASE. A client that cannot reach a daemon is upstream's own shape, and
    # it is the one a `command -v docker` probe passes. Without this case the FR3 choice is
    # untested and the cheaper probe would score identically.
    _stub docker 1
    _case "a docker client with no reachable daemon fails" 1 ensure_capability docker
    _stub docker 0
    _case "a reachable docker daemon passes" 0 ensure_capability docker

    _case "several capabilities at once pass together" 0 main docker jq unzip
    "$rm_bin" -f "$bin/jq"
    _case "one missing capability among several fails the set" 1 main docker jq unzip

    # The INSTALL branch, driven end to end: a fake passwordless `sudo` runs a fake `apt-get`
    # that materialises the package. Without this, `CI_PREFLIGHT_INSTALL=never` above would be
    # the only path ever exercised and the install branch would be unproven code.
    CI_PREFLIGHT_INSTALL=auto
    printf '#!/bin/sh\n[ "$1" = "-n" ] && exit 0\nexec "$@"\n' > "$bin/sudo"
    "$chmod_bin" +x "$bin/sudo"
    printf '#!/bin/sh\nfor a in "$@"; do [ "$a" = "jq" ] && { printf "#!/bin/sh\\nexit 0\\n" > "%s/jq"; "%s" +x "%s/jq"; }; done\nexit 0\n' \
        "$bin" "$chmod_bin" "$bin" > "$bin/apt-get"
    "$chmod_bin" +x "$bin/apt-get"
    _case "a missing but installable capability is installed and passes" 0 ensure_capability jq

    # ⚠️ The ROOT branch, which the `sudo` case above does NOT reach. With `id` off the
    # synthetic PATH, `$(id -u)` is empty and the root test is false unconditionally -- so
    # without this stub half of FR4 was unproven code. `sudo` is removed so the branch cannot
    # be satisfied by the other path.
    "$rm_bin" -f "$bin/jq" "$bin/sudo"
    printf '#!/bin/sh\nprintf 0\n' > "$bin/id"
    "$chmod_bin" +x "$bin/id"
    _case "a root job installs a missing capability without sudo" 0 ensure_capability jq

    # 🔴 The four install-failure causes must be told APART, which is the whole premise of this
    # ticket applied to this script's own failure path. One message for four causes is a
    # wrong-cause message three times in four, and the remedy being identical is exactly what
    # made it cheap to ship: a reader sent to check `sudo` when the mirror is down spends their
    # time on the wrong machine.
    # ⚠️ `docker` goes too: it is still stubbed present from the probe cases above, and a
    # "cause" assertion against a capability that is PRESENT asserts nothing.
    "$rm_bin" -f "$bin/id" "$bin/jq" "$bin/docker"
    _says "the no-sudo cause is named" "not root and has no passwordless sudo" \
        ensure_capability jq
    printf '#!/bin/sh\nprintf 0\n' > "$bin/id"
    "$chmod_bin" +x "$bin/id"
    printf '#!/bin/sh\nexit 100\n' > "$bin/apt-get"
    "$chmod_bin" +x "$bin/apt-get"
    _says "a failing package manager is NOT reported as a sudo problem" \
        "the mirror is unreachable" ensure_capability jq
    _says "docker's cause is its own, not an install one" \
        "A job cannot install a daemon" ensure_capability docker

    # 🔴 The install REPORTS SUCCESS and the capability is still absent -- a package that lands
    # outside this step's PATH, or a post-install that fails quietly. Without its own branch the
    # message falls back to the last FAILING attempt's reason, which in a job naming several
    # capabilities is a wrong cause assembled out of a right one.
    printf '#!/bin/sh\nexit 0\n' > "$bin/apt-get"
    "$chmod_bin" +x "$bin/apt-get"
    _says "an install that succeeds while the tool stays absent says so" \
        "reported SUCCESS" ensure_capability jq
    # ⚠️ And it must not inherit the PREVIOUS capability's reason. `docker` fails first, setting
    # its own; `jq` then takes the branch above and must not print docker's.
    # ⚠️ A FUNCTION, not `sh -c`: the synthetic PATH has no `sh`, and a subshell would not
    # share the variable this case exists to observe.
    _docker_then_jq() {
        ensure_capability docker >/dev/null 2>&1
        ensure_capability jq
    }
    _says "a later capability does not inherit an earlier one's cause" \
        "reported SUCCESS" _docker_then_jq


    # Put a working installer back for the control below.
    printf '#!/bin/sh\nfor a in "$@"; do [ "$a" = "jq" ] && { printf "#!/bin/sh\\nexit 0\\n" > "%s/jq"; "%s" +x "%s/jq"; }; done\nexit 0\n' \
        "$bin" "$chmod_bin" "$bin" > "$bin/apt-get"
    "$chmod_bin" +x "$bin/apt-get"

    # 🔴 THE NEGATIVE CONTROL FOR THE INSTALL BRANCH. Docker carries no package on purpose, so
    # it must still fail here -- in an environment where installing IS possible, which the case
    # immediately above just proved (root, with a working `apt-get`; `sudo` was removed there
    # and stays removed). Without this case a script that installed everything it was asked for
    # would pass every case above.
    # ⚠️ The stub is REMOVED first. Left in place the case passes because docker is present,
    # which says nothing about whether an install was attempted -- a control that controls for
    # nothing is worse than none, because it is counted.
    "$rm_bin" -f "$bin/docker"
    _case "docker is never installed, even where the box allows installs" 1 ensure_capability docker

    PATH="$saved_path"
    CI_PREFLIGHT_INSTALL="$saved_install"

    if [ "$failures" = "0" ]; then
        echo "self-test: all cases passed"
        return 0
    fi
    echo "self-test: $failures case(s) failed"
    return 1
}

if [ "${1:-}" = "--self-test" ]; then
    self_test
else
    main "$@"
fi
