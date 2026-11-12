"""Side-effect class of a shell command, from a static allow-list.

A shell tool cannot be classified by its name: `ls` and `rm -rf` are the same tool. This
module reads the command instead. It answers "read" only when every part of the whole
command is a listed read-only program used without a flag that writes or runs something.
Anything it cannot fully account for is "unknown". It never answers a write class: a
command it does not recognise needs a person, not a guess.

The rules live in side_effects.json under "_shell" and are edited by people only.
"""
import re
import shlex

# Constructs that run other commands or cannot be followed statically.
_OPAQUE = ("`", "$(", "<(", ">(", "\n", "\r")
# Harmless stderr plumbing, removed before parsing.
_STDERR = re.compile(r"\s*2>\s*(?:/dev/null|&1)(?=\s|$|[;|&])")
_JOINERS = {"|", "&&", "||", ";"}
_PUNCT = set("();<>|&")


def segments(command):
    """Split a command into its simple commands (lists of words), or None if it holds
    anything other than plain commands joined by | && || ;"""
    if not isinstance(command, str) or not command.strip():
        return None
    command = command.strip()
    if any(x in command for x in _OPAQUE):
        return None
    lex = shlex.shlex(_STDERR.sub("", command), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for tok in tokens:
        if tok in _JOINERS:
            if not cur:
                return None
            out.append(cur)
            cur = []
        elif tok and set(tok) <= _PUNCT:
            return None  # a redirect, background job, subshell or unknown operator
        else:
            cur.append(tok)
    if not cur:
        return None
    out.append(cur)
    return out


def _read_only(words, rules):
    head, args = words[0], words[1:]
    rule = rules.get(head)
    if rule is None:  # also covers paths (/bin/ls) and VAR=value prefixes
        return False
    if "sub" in rule:
        if not args or args[0] not in rule["sub"]:
            return False
        args = args[1:]
    for a in args:
        if any(a.startswith(d) for d in rule.get("deny", ())):
            return False
        if a.startswith("-") and not a.startswith("--") and set(a[1:]) & set(rule.get("deny_short", "")):
            return False
    if "max_args" in rule and sum(1 for a in args if not a.startswith("-")) > rule["max_args"]:
        return False
    return True


def classify(command, rules):
    """"read" if the whole command is provably read-only under the rules, else "unknown"."""
    segs = segments(command)
    if not segs:
        return "unknown"
    return "read" if all(_read_only(s, rules) for s in segs) else "unknown"


def shape(command):
    """The programs a command runs, in order, for counting: "cd && git | head"."""
    segs = segments(command)
    if not segs:
        return "(not plain commands)"
    return " ; ".join(s[0] for s in segs)
