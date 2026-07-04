#!/usr/bin/env python3
"""
OnTrack Companion  -  a personal OnTrack (Doubtfire) dashboard + reminder tool.

Runs on a Synology DS220j (or any machine with Python 3, no extra packages).
It reuses a session token you copy once from your browser, so you don't log in
every time. It can:

    test       - check your token works and list your units
    dump       - save raw API JSON to files (used to confirm field names)
    dashboard  - build index.html showing every unit + task, sorted by urgency
    remind     - email you a digest of tasks to START and tasks DUE soon/overdue
    all        - dashboard + remind (what the daily scheduled task should run)

Nothing here bypasses Deakin login. You authenticate normally in a browser,
then paste the session token into config.ini. The token generally keeps
refreshing itself while this tool polls; if it ever lapses, paste a fresh one.

Author: built for Rutwik. Zero third-party dependencies (Python standard lib only).
"""

import sys
import os
import json
import ssl
import smtplib
import base64
import configparser
from datetime import datetime, date, timedelta, timezone as _dt_timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib import request, parse
from urllib.error import HTTPError, URLError

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")
STATE_PATH = os.path.join(HERE, "state.json")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("No config.ini found next to this script. Copy config.example.ini "
                 "to config.ini and fill it in.")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    apply_env_overrides(cfg)
    return cfg


# Maps environment variable names -> (section, key) in config.ini. Lets real
# secrets come from GitHub Actions Secrets (or any environment) instead of
# being committed to a public repo's config.ini, which stays safe to commit
# with these fields left blank.
ENV_OVERRIDES = {
    "ONTRACK_AUTH_TOKEN": ("ontrack", "auth_token"),
    "ONTRACK_USERNAME": ("ontrack", "username"),
    "SMTP_USER": ("email", "smtp_user"),
    "SMTP_PASSWORD": ("email", "smtp_password"),
    "EMAIL_FROM": ("email", "from_addr"),
    "EMAIL_TO": ("email", "to_addr"),
    "GITHUB_PUSH_TOKEN": ("github", "token"),
    "GIST_ID": ("github", "gist_id"),
}


def apply_env_overrides(cfg):
    for env_name, (section, key) in ENV_OVERRIDES.items():
        val = os.environ.get(env_name)
        if val:
            if not cfg.has_section(section):
                cfg.add_section(section)
            cfg.set(section, key, val)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class OnTrack:
    """Minimal client for the Doubtfire/OnTrack REST API.

    Auth is deliberately flexible because the exact scheme can differ between
    OnTrack versions. It first tries the Authorization header (what current
    OnTrack uses), and if that returns 401 it retries with the older
    ?username=&auth_token= query-string style. Whichever works is remembered.
    """

    def __init__(self, cfg):
        self.base = cfg.get("ontrack", "base_url").rstrip("/")
        self.token = cfg.get("ontrack", "auth_token").strip()
        self.username = cfg.get("ontrack", "username", fallback="").strip()
        self.header_name = cfg.get("ontrack", "auth_header_name", fallback="Authorization").strip()
        insecure = cfg.getboolean("ontrack", "insecure_skip_verify", fallback=False)
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        # 'header' or 'query' - decided on first successful call
        self._mode = None

    def _do(self, url, mode):
        headers = {"Accept": "application/json",
                   "User-Agent": "OnTrackCompanion/1.0 (personal use)"}
        if mode == "header":
            headers[self.header_name] = self.token
            # OnTrack's own web client sends the username as its own header
            # alongside the token - without it some instances reject the
            # request (seen as an HTTP 419), so mirror that behaviour.
            if self.username:
                headers["Username"] = self.username
        elif mode == "query":
            sep = "&" if "?" in url else "?"
            qp = {"auth_token": self.token}
            if self.username:
                qp["username"] = self.username
            url = url + sep + parse.urlencode(qp)
        req = request.Request(url, headers=headers)
        with request.urlopen(req, context=self.ctx, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path):
        url = self.base + path
        # If we already know which auth mode works, use it directly.
        modes = [self._mode] if self._mode else ["header", "query"]
        last_err = None
        for mode in modes:
            try:
                data = self._do(url, mode)
                self._mode = mode
                return data
            except HTTPError as e:
                last_err = e
                # 401/403 = rejected credentials; 419 = some OnTrack/Rails
                # instances use this for "session/auth timeout". Try the
                # other auth mode before giving up on either.
                if e.code in (401, 403, 419) and self._mode is None:
                    continue  # try the next auth mode
                raise
        raise last_err

    # --- typed helpers -----------------------------------------------------

    def projects(self):
        """List of the student's enrolled projects (units)."""
        return self.get("/projects")

    def project(self, project_id):
        """Full project including its tasks."""
        return self.get("/projects/%s" % project_id)

    def task_definitions(self, unit_id):
        """Task definitions for a unit (task names, abbreviations, base dates).

        Students can see these; the exact endpoint has moved between versions,
        so we try the common shapes and return whichever yields a list.
        """
        candidates = [
            "/units/%s/task_definitions" % unit_id,
            "/task_definitions?unit_id=%s" % unit_id,
        ]
        for path in candidates:
            try:
                data = self.get(path)
                if isinstance(data, list):
                    return data
            except HTTPError:
                continue
        # Fall back to reading them off the full unit object.
        try:
            unit = self.get("/units/%s" % unit_id)
            if isinstance(unit, dict) and isinstance(unit.get("task_definitions"), list):
                return unit["task_definitions"]
        except HTTPError:
            pass
        return []


    def task_comments(self, project_id, task_definition_id):
        """The comment thread (feedback) for one task, oldest first."""
        path = "/projects/%s/task_def_id/%s/comments" % (project_id, task_definition_id)
        try:
            data = self.get(path)
            return data if isinstance(data, list) else []
        except HTTPError as e:
            if e.code == 404:
                return []
            raise


# ---------------------------------------------------------------------------
# GitHub (dashboard hosting + token relay via a Gist)
# ---------------------------------------------------------------------------

class GitHubAPI:
    """Minimal GitHub REST client, used for two unrelated things:

    1. Pushing the dashboard HTML to a repo so GitHub Pages can serve it -
       genuinely 24/7, no load on the NAS at all once pushed.
    2. Reading a Gist that the browser bookmarklet writes a fresh OnTrack
       token into, so a scheduled run can self-heal its token without ever
       touching the NAS's Task Scheduler or File Station.

    Reading a Gist needs no authentication (GitHub allows anyone with the ID
    to read one, public or "secret" - secret just means unlisted). Pushing
    the dashboard file does need a token, kept only in config.ini, never in
    the browser.
    """

    def __init__(self, cfg):
        self.token = cfg.get("github", "token", fallback="").strip()
        if not self.token:
            # GitHub Actions injects this automatically for the repo a
            # workflow runs in - no manual PAT needed when running there.
            self.token = os.environ.get("GITHUB_TOKEN", "").strip()
        self.owner = cfg.get("github", "repo_owner", fallback="").strip()
        self.repo = cfg.get("github", "repo_name", fallback="").strip()
        if not self.owner or not self.repo:
            # Actions always sets this ("owner/repo") without needing to be
            # declared in the workflow's env: block - use it if config.ini
            # doesn't specify one explicitly (e.g. when run outside Actions).
            gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
            if "/" in gh_repo:
                self.owner, self.repo = gh_repo.split("/", 1)
        self.branch = cfg.get("github", "branch", fallback="main").strip() or "main"
        self.gist_id = cfg.get("github", "gist_id", fallback="").strip()
        self.gist_filename = cfg.get("github", "gist_filename",
                                     fallback="ontrack_token.txt").strip()
        self.ctx = ssl.create_default_context()

    def _request(self, url, method="GET", body=None, use_auth=True):
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "OnTrackCompanion/1.0"}
        if use_auth and self.token:
            headers["Authorization"] = "token %s" % self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        with request.urlopen(req, context=self.ctx, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def put_file(self, path, content_bytes, message):
        """Create or update a file in the repo (used for the dashboard HTML)."""
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (self.owner, self.repo, path)
        sha = None
        try:
            existing = self._request(url + "?ref=" + self.branch)
            sha = existing.get("sha")
        except HTTPError as e:
            if e.code != 404:
                raise
        body = {"message": message,
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "branch": self.branch}
        if sha:
            body["sha"] = sha
        self._request(url, method="PUT", body=body)

    def get_gist_token(self):
        """Read whatever token the bookmarklet last pushed, or None.

        Deliberately unauthenticated: reading a gist (public or secret)
        needs no token at all. In GitHub Actions, self.token is the
        repo-scoped GITHUB_TOKEN, which has zero permission over Gists -
        sending it here causes GitHub to reject the request with 403,
        rather than just ignoring it. Always read anonymously instead.
        """
        if not self.gist_id:
            return None
        url = "https://api.github.com/gists/%s" % self.gist_id
        data = self._request(url, use_auth=False)
        f = (data.get("files") or {}).get(self.gist_filename)
        if not f:
            return None
        return (f.get("content") or "").strip()


def sync_token_from_gist(cfg):
    """If a Gist is configured, pick up a fresher token the bookmarklet may
    have pushed since the last run. Fails silently and keeps the existing
    token if GitHub is unreachable or nothing's configured - this must never
    be the reason a scheduled run breaks."""
    if not cfg.has_section("github"):
        return
    gist_id = cfg.get("github", "gist_id", fallback="").strip()
    if not gist_id:
        return
    try:
        gh = GitHubAPI(cfg)
        remote = gh.get_gist_token()
        current = cfg.get("ontrack", "auth_token", fallback="").strip()
        if remote and remote != current:
            write_token(remote)
            cfg.set("ontrack", "auth_token", remote)
            print("Picked up a fresher OnTrack token from GitHub.")
    except Exception as e:
        print("Could not check for a fresher token (using existing one): %s" % e)


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

# Truly finished (including portfolio-assessed tasks, which are already graded).
DONE_STATES = {"complete", "feedback_exceeded", "assess_in_portfolio"}

# Submitted and sitting with the tutor - no action needed from you right now.
REVIEW_STATES = {"ready_for_feedback"}

# The ball is in YOUR court: these have stalled waiting on you to do something
# (resubmit, redo, book a discussion, demonstrate, or ask for help).
ACTION_STATES = {"fix_and_resubmit", "redo", "discuss", "demonstrate", "need_help"}

# For due-date nagging, skip anything that's submitted, in review, done, or
# already surfaced in the "needs your action" list (avoids double-listing).
NO_DUE_NAG = DONE_STATES | REVIEW_STATES | ACTION_STATES

STATUS_LABELS = {
    "not_started": "Not started",
    "working_on_it": "Working on it",
    "need_help": "Need help",
    "ready_for_feedback": "Ready for feedback",
    "fix_and_resubmit": "Fix and resubmit",
    "redo": "Redo",
    "discuss": "Discuss",
    "demonstrate": "Demonstrate",
    "complete": "Complete",
    "fail": "Fail",
    "time_exceeded": "Time exceeded",
    "do_not_resubmit": "Do not resubmit",
    "feedback_exceeded": "Feedback exceeded",
    "assess_in_portfolio": "Assessed in portfolio",
}


def melbourne_now():
    """Current time in Melbourne (AEST/AEDT), computed from real UTC so it's
    correct no matter what timezone the NAS itself is set to. Uses Australia's
    actual DST rule (first Sunday of October to first Sunday of April) with
    pure stdlib - no zoneinfo/tzdata dependency needed."""
    utc_now = datetime.now(_dt_timezone.utc)

    def first_sunday(year, month):
        d = datetime(year, month, 1, tzinfo=_dt_timezone.utc)
        return d + timedelta(days=(6 - d.weekday()) % 7)

    year = utc_now.year
    dst_start = first_sunday(year, 10)   # DST begins (AEDT, UTC+11)
    dst_end = first_sunday(year, 4)      # DST ends (back to AEST, UTC+10)
    in_dst = utc_now >= dst_start or utc_now < dst_end
    return utc_now + timedelta(hours=11 if in_dst else 10)


def melbourne_today():
    return melbourne_now().date()


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_datetime(s):
    """Parse OnTrack's created_at timestamp. Tolerant of the trailing 'Z' that
    Python's fromisoformat only handles from 3.11 onward."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return None


def fmt_12h_time(dt):
    """'%I:%M %p' but without the leading zero on the hour (e.g. '5:22 PM')."""
    return dt.strftime("%I:%M %p").lstrip("0")


def normalize_comments(raw):
    """Turn the API's raw comment list into simple dicts for display."""
    out = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        author = c.get("author") or {}
        name = (" ".join(filter(None, [author.get("first_name"),
                                       author.get("last_name")]))).strip() or "Unknown"
        ctype = c.get("type") or "text"
        dt = parse_datetime(c.get("created_at"))
        out.append({
            "author": name,
            "type": ctype,
            "text": c.get("comment") or "",
            "when": dt,
            "when_display": ("%s, %s" % (dt.strftime("%d %b"), fmt_12h_time(dt))
                             if dt else (c.get("created_at") or "")),
            "is_new": bool(c.get("is_new")),
        })
    return out


def collect_tasks(api, verbose=False, fetch_comments=True):
    """Return (units, tasks) as normalized dicts ready for display/reminders."""
    projects = api.projects()
    if isinstance(projects, dict):
        projects = projects.get("projects", [])  # tolerate wrapper shapes

    units = []
    tasks = []
    defs_cache = {}

    for p in projects:
        unit = p.get("unit") or {}
        unit_id = p.get("unit_id") or unit.get("id")
        unit_code = unit.get("code", "?")
        unit_name = unit.get("name", "")
        target_grade = p.get("target_grade")
        units.append({"code": unit_code, "name": unit_name,
                      "target_grade": target_grade})
        if verbose:
            print("  unit %-12s %s" % (unit_code, unit_name))

        # Load task definitions once per unit (names live here, not on the task).
        if unit_id not in defs_cache:
            tds = api.task_definitions(unit_id)
            defs_cache[unit_id] = {td.get("id"): td for td in tds if isinstance(td, dict)}

        # Full project carries the student's tasks with target dates + status.
        full = api.project(p["id"])
        seen_def_ids = set()
        for t in full.get("tasks", []):
            def_id = t.get("task_definition_id")
            seen_def_ids.add(def_id)
            td = defs_cache[unit_id].get(def_id, {})
            comments = []
            if fetch_comments:
                try:
                    raw = api.task_comments(p["id"], def_id)
                    comments = normalize_comments(raw)
                except HTTPError:
                    comments = []  # don't let one bad task break the whole run
            tasks.append({
                "unit_code": unit_code,
                "abbr": td.get("abbreviation", "?"),
                "name": td.get("name", "Task %s" % def_id),
                "status": t.get("status", "not_started"),
                # target_* are already adjusted to the student's target grade
                "start_date": parse_date(t.get("target_start_date") or td.get("start_date")),
                "due_date": parse_date(t.get("target_due_date") or t.get("due_date")
                                       or td.get("target_date")),
                "weight": td.get("weighting"),
                "comments": comments,
            })

        # OnTrack doesn't always create a student task record until you've
        # interacted with it - a task you've never touched can be missing
        # entirely, even though its definition exists. Fill those in as
        # "Not started" using the definition's own dates, so nothing you
        # haven't started yet silently disappears from the list.
        for def_id, td in defs_cache[unit_id].items():
            if def_id in seen_def_ids or not isinstance(td, dict):
                continue
            tasks.append({
                "unit_code": unit_code,
                "abbr": td.get("abbreviation", "?"),
                "name": td.get("name", "Task %s" % def_id),
                "status": "not_started",
                "start_date": parse_date(td.get("start_date")),
                "due_date": parse_date(td.get("target_date")),
                "weight": td.get("weighting"),
                "comments": [],
            })

    return units, tasks


# ---------------------------------------------------------------------------
# Reminder logic
# ---------------------------------------------------------------------------

def build_digest(tasks, start_lead, due_lead):
    today = melbourne_today()
    to_start, awaiting_action, overdue, due_soon, new_feedback = [], [], [], [], []
    for t in tasks:
        # Unread comments (marker feedback you haven't seen yet)
        unread = [c for c in t.get("comments", []) if c.get("is_new")]
        if unread:
            t2 = dict(t)
            t2["latest_comment"] = unread[-1]
            new_feedback.append(t2)
        # Needs your action (resubmit / redo / discuss / demonstrate / need help)
        if t["status"] in ACTION_STATES:
            awaiting_action.append(t)
            continue
        # Should start
        if (t["status"] == "not_started" and t["start_date"]
                and t["start_date"] <= today + timedelta(days=start_lead)
                and not (t["due_date"] and t["due_date"] < today)):
            to_start.append(t)
        # Due / overdue (skip if submitted, in review, or done)
        if t["status"] not in NO_DUE_NAG and t["due_date"]:
            if t["due_date"] < today:
                overdue.append(t)
            elif t["due_date"] <= today + timedelta(days=due_lead):
                due_soon.append(t)
    for lst in (to_start, awaiting_action, overdue, due_soon):
        lst.sort(key=lambda x: (x["due_date"] or date.max, x["unit_code"]))
    new_feedback.sort(key=lambda x: x["latest_comment"]["when"] or datetime.min,
                      reverse=True)
    return to_start, awaiting_action, overdue, due_soon, new_feedback


def digest_has_content(digest):
    return any(len(x) for x in digest)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(cfg, subject, html_body, text_body):
    host = cfg.get("email", "smtp_host")
    port = cfg.getint("email", "smtp_port", fallback=587)
    user = cfg.get("email", "smtp_user")
    pw = cfg.get("email", "smtp_password")
    sender = cfg.get("email", "from_addr", fallback=user)
    to_addr = cfg.get("email", "to_addr")
    use_ssl = cfg.getboolean("email", "use_ssl", fallback=(port == 465))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(sender, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(user, pw)
            s.sendmail(sender, [to_addr], msg.as_string())


def render_digest_email(digest, notice=None):
    to_start, awaiting_action, overdue, due_soon, new_feedback = digest
    today = melbourne_today()

    def days_label(d):
        if not d:
            return ""
        n = (d - today).days
        if n < 0:
            return "%d day%s overdue" % (-n, "s" if -n != 1 else "")
        if n == 0:
            return "due today"
        return "in %d day%s" % (n, "s" if n != 1 else "")

    def right_col(t, show_status):
        if show_status:
            return STATUS_LABELS.get(t["status"], t["status"])
        return days_label(t["due_date"])

    def rows_text(items, show_status=False):
        out = []
        for t in items:
            when = t["due_date"].strftime("%a %d %b") if t["due_date"] else "no date"
            out.append("  - [%s] %s %s  (%s, %s)" % (
                t["unit_code"], t["abbr"], t["name"], when, right_col(t, show_status)))
        return "\n".join(out) or "  (none)"

    def excerpt(text, n=180):
        text = " ".join((text or "").split())
        return text if len(text) <= n else text[:n].rstrip() + "\u2026"

    def feedback_body(c):
        if c["type"] != "text":
            return "[%s attachment \u2014 open OnTrack to view]" % c["type"]
        return "\u201c%s\u201d" % excerpt(c["text"])

    def feedback_text():
        if not new_feedback:
            return "  (none)"
        lines = []
        for t in new_feedback:
            c = t["latest_comment"]
            lines.append("  - [%s] %s %s \u2014 %s (%s): %s" % (
                t["unit_code"], t["abbr"], t["name"], c["author"],
                c["when_display"], feedback_body(c)))
        return "\n".join(lines)

    text = "OnTrack reminders for %s\n\n" % today.strftime("%A %d %B %Y")
    if notice:
        text += "! %s\n\n" % notice
    text += "NEW FEEDBACK FROM YOUR MARKER:\n%s\n\n" % feedback_text()
    text += "NEEDS YOUR ACTION (resubmit / discuss / demonstrate):\n%s\n\n" % rows_text(awaiting_action, True)
    text += "START NOW:\n%s\n\n" % rows_text(to_start)
    text += "OVERDUE:\n%s\n\n" % rows_text(overdue)
    text += "DUE SOON:\n%s\n" % rows_text(due_soon)

    def rows_html(items, accent, show_status=False):
        if not items:
            return '<tr><td style="padding:6px 0;color:#8a8f98">Nothing here.</td></tr>'
        out = ""
        for t in items:
            when = t["due_date"].strftime("%a %d %b") if t["due_date"] else "no date"
            out += (
                '<tr>'
                '<td style="padding:8px 10px;border-left:3px solid %s;font-family:monospace;'
                'font-weight:600;color:#1b1f24;white-space:nowrap">%s</td>'
                '<td style="padding:8px 10px;color:#1b1f24">%s <span style="color:#6b7280">%s</span></td>'
                '<td style="padding:8px 10px;color:#374151;white-space:nowrap">%s</td>'
                '<td style="padding:8px 10px;color:%s;white-space:nowrap;font-weight:600">%s</td>'
                '</tr>' % (accent, t["unit_code"], t["abbr"], t["name"], when,
                          accent, right_col(t, show_status))
            )
        return out

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def feedback_html():
        if not new_feedback:
            return '<p style="color:#8a8f98;font-size:13px;margin:4px 0">Nothing new.</p>'
        out = ""
        for t in new_feedback:
            c = t["latest_comment"]
            body = ('<span style="color:#8a8f98;font-style:italic">%s</span>' % esc(feedback_body(c))
                   if c["type"] != "text" else "&ldquo;%s&rdquo;" % esc(excerpt(c["text"])))
            out += (
                '<div style="border-left:3px solid #2f6df6;padding:6px 12px;margin:6px 0;'
                'background:#f5f8ff">'
                '<div style="font-size:13px;color:#1b1f24"><b>%s</b> %s '
                '<span style="color:#6b7280">&middot; %s &middot; %s</span></div>'
                '<div style="font-size:14px;color:#374151;margin-top:3px">%s</div>'
                '</div>' % (t["unit_code"], esc(t["name"]), esc(c["author"]),
                           esc(c["when_display"]), body)
            )
        return out

    def section(title, items, accent, show_status=False):
        return (
            '<h3 style="margin:22px 0 6px;font-size:14px;letter-spacing:.04em;'
            'text-transform:uppercase;color:%s">%s <span style="color:#9aa0a6">(%d)</span></h3>'
            '<table style="width:100%%;border-collapse:collapse;font-size:14px">%s</table>'
            % (accent, title, len(items), rows_html(items, accent, show_status))
        )

    html = (
        '<div style="max-width:640px;margin:0 auto;font-family:-apple-system,'
        'Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1b1f24">'
        '<p style="font-size:13px;color:#6b7280;margin:0 0 4px">OnTrack Companion</p>'
        '<h2 style="margin:0 0 4px;font-size:20px">Reminders for %s</h2>'
        % today.strftime("%A %d %B")
        + ('<p style="background:#fff7ed;border:1px solid #f6c88a;border-radius:8px;'
           'padding:8px 12px;font-size:13px;color:#92400e;margin:8px 0">%s</p>' % notice
           if notice else "")
        + '<h3 style="margin:22px 0 6px;font-size:14px;letter-spacing:.04em;'
          'text-transform:uppercase;color:#2f6df6">New feedback from your marker '
          '<span style="color:#9aa0a6">(%d)</span></h3>%s' % (len(new_feedback), feedback_html())
        + section("Needs your action", awaiting_action, "#7c3aed", show_status=True)
        + section("Start now", to_start, "#b45309")
        + section("Overdue", overdue, "#b91c1c")
        + section("Due soon", due_soon, "#047857")
        + '<p style="margin:24px 0 0;font-size:12px;color:#9aa0a6">'
          'Open OnTrack to submit. This is an automated note from your Synology.</p>'
        '</div>'
    )
    return text, html


# ---------------------------------------------------------------------------
# Dashboard (static index.html)
# ---------------------------------------------------------------------------

def render_dashboard(units, tasks, out_path, current_units=None):
    today = melbourne_today()

    def bucket(t):
        if t["status"] in ACTION_STATES:
            return "action"
        if t["status"] in DONE_STATES:
            return "done"
        if t["status"] in REVIEW_STATES:
            return "review"
        if t["due_date"] and t["due_date"] < today:
            return "overdue"
        if t["due_date"] and t["due_date"] <= today + timedelta(days=3):
            return "soon"
        return "upcoming"

    # Action first (needs you), then overdue, due-soon, upcoming, review, done.
    order = {"action": 0, "overdue": 1, "soon": 2, "upcoming": 3,
             "review": 4, "done": 5}
    tasks_sorted = sorted(
        tasks,
        key=lambda t: (order[bucket(t)], t["due_date"] or date.max, t["unit_code"]))

    counts = {"action": 0, "overdue": 0, "soon": 0, "upcoming": 0,
              "review": 0, "done": 0}
    for t in tasks:
        counts[bucket(t)] += 1

    def days_badge(t, b):
        if b in ("done", "review"):
            return '<span class="days none">\u2713</span>'
        if not t["due_date"]:
            return '<span class="days none">—</span>'
        n = (t["due_date"] - today).days
        if n < 0:
            return '<span class="days over">%dd late</span>' % -n
        if n == 0:
            return '<span class="days today">today</span>'
        return '<span class="days">%dd</span>' % n

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def comment_thread_html(comments):
        if not comments:
            return '<p class="cmt-empty">No comments on this task yet.</p>'
        out = []
        for c in comments:
            if c["type"] == "text":
                body = '<div class="cmt-body">%s</div>' % esc(c["text"]).replace("\n", "<br>")
            else:
                body = '<div class="cmt-body cmt-attach">[%s attachment \u2014 open OnTrack to view]</div>' % esc(c["type"])
            new_flag = ' <span class="cmt-new">new</span>' if c["is_new"] else ""
            out.append(
                '<div class="cmt">'
                '<div class="cmt-head"><span class="cmt-author">%s</span>%s'
                '<span class="cmt-date">%s</span></div>%s</div>'
                % (esc(c["author"]), new_flag, esc(c["when_display"]), body)
            )
        return "".join(out)

    rows = []
    for idx, t in enumerate(tasks_sorted):
        b = bucket(t)
        due = t["due_date"].strftime("%a %d %b") if t["due_date"] else "—"
        start = t["start_date"].strftime("%a %d %b") if t["start_date"] else "—"
        status = STATUS_LABELS.get(t["status"], t["status"])
        comments = t.get("comments", [])
        n_comments = len(comments)
        n_new = sum(1 for c in comments if c["is_new"])
        did = "cmt-%d" % idx
        fb_class = "fb-new" if n_new else ("fb-has" if n_comments else "fb-none")
        fb_label = ("%d new" % n_new) if n_new else (str(n_comments) if n_comments else "0")
        rows.append(
            '<tr class="r %s" data-unit="%s" data-bucket="%s">'
            '<td class="u">%s</td>'
            '<td class="a">%s</td>'
            '<td class="n">%s</td>'
            '<td class="st"><span class="pill %s">%s</span></td>'
            '<td class="sd">%s</td>'
            '<td class="dd">%s</td>'
            '<td class="db">%s</td>'
            '<td class="fb"><button class="fbbtn %s" data-target="%s">\U0001F4AC %s</button></td>'
            '</tr>'
            '<tr class="detail" id="%s" data-unit="%s" style="display:none">'
            '<td colspan="10"><div class="cmt-thread">%s</div></td></tr>'
            % (b, t["unit_code"], b, t["unit_code"], t["abbr"], t["name"],
              t["status"], status, start, due, days_badge(t, b),
              fb_class, did, fb_label, did, t["unit_code"], comment_thread_html(comments))
        )

    all_codes = [u["code"] for u in units]
    current_codes = [c for c in (current_units or []) if c in all_codes]
    other_codes = [c for c in all_codes if c not in current_codes]
    # Order: current-trimester units first, then the rest.
    ordered_codes = current_codes + other_codes

    if current_codes:
        filters_html = (
            '<button class="chip active" data-filter="%s">Current</button>'
            '<button class="chip" data-filter="all">All</button>'
            % ",".join(current_codes)
        )
    else:
        filters_html = '<button class="chip active" data-filter="all">All</button>'
    filters_html += "".join(
        '<button class="chip" data-filter="%s">%s</button>' % (code, code)
        for code in ordered_codes)

    mel_now = melbourne_now()
    updated = mel_now.strftime("%a %d %b %Y, ") + fmt_12h_time(mel_now) + " AEST/AEDT"
    true_epoch = int(datetime.now(_dt_timezone.utc).timestamp())

    html = DASHBOARD_TEMPLATE.format(
        updated=updated, updated_epoch=true_epoch,
        action=counts["action"], overdue=counts["overdue"], soon=counts["soon"],
        upcoming=counts["upcoming"], review=counts["review"], done=counts["done"],
        filters_html=filters_html, rows="".join(rows),
        total=len(tasks), n_units=len(units))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnTrack \u2014 my tasks</title>
<style>
  :root {{
    --ink:#14181d; --sub:#6b7280; --line:#e6e8eb; --bg:#f6f7f9; --card:#ffffff;
    --over:#b91c1c; --soon:#b45309; --up:#047857; --done:#9aa0a6; --accent:#2f6df6;
    --action:#7c3aed;
  }}
  .stale {{ display:none; background:#fff1f1; border:1px solid #f3b4b4; color:#8a1414;
    border-radius:10px; padding:10px 14px; margin-bottom:16px; font-size:14px; }}
  .stale.show {{ display:block; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:28px 18px 60px; }}
  header {{ display:flex; align-items:baseline; justify-content:space-between;
    flex-wrap:wrap; gap:8px; margin-bottom:18px; }}
  h1 {{ font-size:22px; margin:0; letter-spacing:-.01em; }}
  h1 span {{ color:var(--sub); font-weight:500; }}
  .updated {{ font-size:12px; color:var(--sub); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .stats {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:18px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:12px 14px; }}
  .stat .num {{ font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .stat .lbl {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--sub); }}
  .stat.action .num {{ color:var(--action); }}
  .stat.overdue .num {{ color:var(--over); }}
  .stat.soon .num {{ color:var(--soon); }}
  .stat.upcoming .num {{ color:var(--up); }}
  .stat.done .num {{ color:var(--done); }}
  .filters {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }}
  .chip {{ border:1px solid var(--line); background:var(--card); color:var(--ink);
    border-radius:999px; padding:5px 12px; font-size:12px; font-family:ui-monospace,Menlo,monospace;
    cursor:pointer; }}
  .chip.active {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
  .tablecard {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
    overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--sub); font-weight:600; padding:12px 12px; border-bottom:1px solid var(--line); }}
  td {{ padding:12px 12px; border-bottom:1px solid var(--line); vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .u {{ font-family:ui-monospace,Menlo,monospace; font-weight:600; white-space:nowrap; }}
  .a {{ font-family:ui-monospace,Menlo,monospace; color:var(--sub); white-space:nowrap; }}
  .n {{ min-width:180px; }}
  .sd,.dd {{ white-space:nowrap; color:#374151; font-variant-numeric:tabular-nums; }}
  .r.action {{ box-shadow:inset 3px 0 0 var(--action); background:#faf7ff; }}
  .r.overdue {{ box-shadow:inset 3px 0 0 var(--over); }}
  .r.soon {{ box-shadow:inset 3px 0 0 var(--soon); }}
  .r.upcoming {{ box-shadow:inset 3px 0 0 var(--up); }}
  .r.review {{ opacity:.7; }}
  .r.done {{ opacity:.5; }}
  .pill {{ font-size:11px; padding:3px 8px; border-radius:999px; white-space:nowrap;
    background:#eef1f4; color:#3b4149; }}
  .pill.not_started {{ background:#fde8e8; color:#b91c1c; }}
  .pill.working_on_it {{ background:#fef3e2; color:#b45309; }}
  .pill.fix_and_resubmit,.pill.redo,.pill.discuss,.pill.demonstrate,.pill.need_help {{ background:#f1e9ff; color:#6d28d9; }}
  .pill.complete,.pill.ready_for_feedback,.pill.feedback_exceeded {{ background:#e7f6ee; color:#047857; }}
  .days {{ font-variant-numeric:tabular-nums; font-weight:600; font-size:13px; }}
  .days.over {{ color:var(--over); }}
  .days.today {{ color:var(--soon); }}
  .days.none {{ color:var(--sub); font-weight:400; }}
  .db {{ text-align:right; white-space:nowrap; }}
  .fb {{ white-space:nowrap; }}
  .fbbtn {{ border:1px solid var(--line); background:#f4f5f7; color:var(--sub);
    border-radius:999px; padding:4px 10px; font-size:12px; cursor:pointer;
    font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; }}
  .fbbtn.fb-has {{ background:#eef2ff; color:#3b4149; border-color:#dde3f7; }}
  .fbbtn.fb-new {{ background:#fde8e8; color:#b91c1c; border-color:#f3b4b4; font-weight:600; }}
  tr.detail td {{ padding:0; }}
  .cmt-thread {{ padding:14px 18px; background:#fafbfc; border-top:1px dashed var(--line); }}
  .cmt {{ padding:8px 0; border-bottom:1px solid #eef0f2; }}
  .cmt:last-child {{ border-bottom:none; }}
  .cmt-head {{ font-size:12px; color:var(--sub); margin-bottom:3px; }}
  .cmt-author {{ font-weight:700; color:var(--ink); margin-right:6px; }}
  .cmt-date {{ margin-left:6px; }}
  .cmt-new {{ background:#fde8e8; color:#b91c1c; font-size:10px; font-weight:700;
    padding:1px 6px; border-radius:999px; margin-right:6px; text-transform:uppercase; }}
  .cmt-body {{ font-size:14px; color:#26292e; line-height:1.5; }}
  .cmt-attach {{ color:var(--sub); font-style:italic; }}
  .cmt-empty {{ color:var(--sub); font-size:13px; margin:0; }}
  .empty {{ padding:40px; text-align:center; color:var(--sub); }}
  @media (max-width:640px) {{
    .stats {{ grid-template-columns:repeat(3,1fr); }}
    .a,.sd {{ display:none; }} th.a-h,th.sd-h {{ display:none; }}
    .db,th.db-h {{ display:none; }}
    .n {{ min-width:130px; }}
    table {{ font-size:13px; }}
    .fbbtn {{ padding:3px 8px; font-size:11px; }}
    td,th {{ padding:10px 8px; }}
  }}
</style></head>
<body><div class="wrap">
  <header>
    <h1>My OnTrack <span>&middot; {n_units} units &middot; {total} tasks</span></h1>
    <div class="updated">updated {updated}</div>
  </header>

  <div class="stale" id="stale"></div>

  <div class="stats">
    <div class="stat action"><div class="num">{action}</div><div class="lbl">Needs action</div></div>
    <div class="stat overdue"><div class="num">{overdue}</div><div class="lbl">Overdue</div></div>
    <div class="stat soon"><div class="num">{soon}</div><div class="lbl">Due &le; 3 days</div></div>
    <div class="stat upcoming"><div class="num">{upcoming}</div><div class="lbl">Upcoming</div></div>
    <div class="stat done"><div class="num">{done}</div><div class="lbl">Done ({review} in review)</div></div>
  </div>

  <div class="filters">
    {filters_html}
  </div>

  <div class="tablecard">
    <table>
      <thead><tr>
        <th>Unit</th><th class="a-h">Task</th><th>Name</th><th>Status</th>
        <th class="sd-h">Start by</th><th>Due</th><th class="db-h" style="text-align:right">Left</th>
        <th>Feedback</th>
      </tr></thead>
      <tbody id="tb">{rows}</tbody>
    </table>
    <div class="empty" id="empty" style="display:none">No tasks match this filter.</div>
  </div>
</div>
<script>
  // Staleness guard: if this page hasn't been refreshed by the NAS in over a
  // day, the scheduled task may be failing (often an expired token). Say so.
  (function(){{
    var built = {updated_epoch} * 1000;
    var ageH = (Date.now() - built) / 3600000;
    if (ageH > 26) {{
      var el = document.getElementById('stale');
      el.className = 'stale show';
      el.innerHTML = '\u26a0\ufe0f This dashboard is ' + Math.round(ageH) +
        ' hours old. The scheduled refresh on your NAS may be failing \u2014 ' +
        'most likely your OnTrack token expired. Check your email for an alert, ' +
        'or refresh the token (see the README).';
    }}
  }})();

  var chips = document.querySelectorAll('.chip');
  var rows = document.querySelectorAll('#tb tr');

  function applyFilter(f) {{
    var units = (f === 'all') ? null : f.split(',');
    var shown = 0;
    rows.forEach(function(r){{
      var ok = !units || units.indexOf(r.getAttribute('data-unit')) !== -1;
      if (r.classList.contains('detail')) {{
        r.style.display = (ok && r.classList.contains('open')) ? 'table-row' : 'none';
      }} else {{
        r.style.display = ok ? '' : 'none';
        if (ok) shown++;
      }}
    }});
    document.getElementById('empty').style.display = shown ? 'none' : 'block';
  }}

  chips.forEach(function(c){{
    c.addEventListener('click', function(){{
      chips.forEach(function(x){{x.classList.remove('active');}});
      c.classList.add('active');
      applyFilter(c.getAttribute('data-filter'));
    }});
  }});

  // Apply whichever chip is active by default on load (may be "Current" or "All").
  var initialChip = document.querySelector('.chip.active');
  if (initialChip) applyFilter(initialChip.getAttribute('data-filter'));

  document.querySelectorAll('.fbbtn').forEach(function(b){{
    b.addEventListener('click', function(){{
      var el = document.getElementById(b.getAttribute('data-target'));
      var open = el.classList.contains('open');
      el.classList.toggle('open', !open);
      el.style.display = open ? 'none' : 'table-row';
    }});
  }});
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# State (avoid sending the same daily digest twice)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Token helpers (expiry awareness + easy refresh)
# ---------------------------------------------------------------------------

def token_days_remaining(token):
    """Best-effort: if the token is a JWT with an 'exp' claim, return the whole
    number of days left; otherwise None (many OnTrack tokens aren't JWTs)."""
    import base64
    try:
        raw = token.strip().split()[-1]          # drop a "Bearer " prefix if any
        parts = raw.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        exp = data.get("exp")
        if not exp:
            return None
        return int((exp - datetime.now().timestamp()) // 86400)
    except Exception:
        return None


def write_token(new_token):
    """Swap only the auth_token line in config.ini, preserving comments/layout."""
    with open(CONFIG_PATH) as f:
        lines = f.read().splitlines()
    out, done = [], False
    for ln in lines:
        if not done and ln.strip().lower().startswith("auth_token"):
            out.append("auth_token = %s" % new_token)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append("auth_token = %s" % new_token)
    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(out) + "\n")


def send_auth_alert(cfg, detail):
    """Email a 'token expired, please refresh' alert. This does NOT need the
    OnTrack token (it uses SMTP), so it still gets through when auth is dead.
    Sends at most once per day."""
    state = load_state()
    today_iso = melbourne_today().isoformat()
    if state.get("last_alert_date") == today_iso:
        return False
    prefix = cfg.get("email", "subject_prefix", fallback="OnTrack")
    subject = prefix + " \u26a0\ufe0f can't reach your account \u2014 refresh token"
    steps = ('1. Log in to https://ontrack.deakin.edu.au in your browser.\n'
             '2. Grab a fresh token (README \u2192 "Getting your token", or the bookmarklet).\n'
             '3. On the NAS run:  python3 ontrack_companion.py settoken "<paste token>"')
    text = ("OnTrack Companion couldn't log in.\n\n%s\n\n"
            "Your session token has most likely expired, so reminders and the "
            "dashboard are paused until you refresh it:\n\n%s\n" % (detail, steps))
    html = (
        '<div style="max-width:560px;margin:0 auto;font-family:-apple-system,'
        'Segoe UI,Roboto,Arial,sans-serif;color:#1b1f24">'
        '<h2 style="color:#b91c1c;margin:0 0 8px">\u26a0\ufe0f OnTrack reminders are paused</h2>'
        '<p style="margin:0 0 10px;font-size:14px">%s</p>'
        '<p style="margin:0 0 6px;font-size:14px">Your token has most likely expired. '
        'To get reminders flowing again:</p>'
        '<ol style="font-size:14px;line-height:1.6;padding-left:20px">'
        '<li>Log in to <a href="https://ontrack.deakin.edu.au">OnTrack</a> in your browser.</li>'
        '<li>Grab a fresh token (bookmarklet or README steps).</li>'
        '<li>On the NAS: <code>python3 ontrack_companion.py settoken "&lt;token&gt;"</code></li>'
        '</ol></div>' % detail
    )
    try:
        send_email(cfg, subject, html, text)
        state["last_alert_date"] = today_iso
        save_state(state)
        return True
    except Exception as e:
        print("Could not send auth alert email: %s" % e)
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def gather(cfg):
    """Single place that decides whether to pull comment text, so 'all' does
    one data pass and shares it between the dashboard and the reminder email."""
    sync_token_from_gist(cfg)
    api = OnTrack(cfg)
    fetch_comments = cfg.getboolean("dashboard", "fetch_comments", fallback=True)
    return collect_tasks(api, fetch_comments=fetch_comments)


def cmd_test(cfg):
    sync_token_from_gist(cfg)
    api = OnTrack(cfg)
    print("Checking token against %s ..." % api.base)
    units, tasks = collect_tasks(api, verbose=True, fetch_comments=False)
    print("\nOK: %d unit(s), %d task(s) visible. Auth mode: %s"
          % (len(units), len(tasks), api._mode))
    days = token_days_remaining(cfg.get("ontrack", "auth_token"))
    if days is not None:
        print("Token is a JWT and looks valid for about %d more day(s)." % days)
    else:
        print("Token expiry isn't readable (not a JWT) \u2014 you'll rely on the "
              "auto alert email + stale-dashboard banner if it lapses.")


def cmd_settoken(cfg):
    if len(sys.argv) < 3 or not sys.argv[2].strip():
        sys.exit('Usage: python3 ontrack_companion.py settoken "<token value>"')
    write_token(sys.argv[2].strip())
    print("Token saved to config.ini. Verifying ...")
    try:
        api = OnTrack(load_config())
        units, tasks = collect_tasks(api, fetch_comments=False)
        print("Verified: %d unit(s), %d task(s) now visible." % (len(units), len(tasks)))
    except HTTPError as e:
        if e.code in (401, 403, 419):
            sys.exit("Saved, but the new token was rejected (%d). Double-check you "
                     "copied the whole value while logged in." % e.code)
        raise


def cmd_dump(cfg):
    api = OnTrack(cfg)
    projects = api.projects()
    with open(os.path.join(HERE, "dump_projects.json"), "w") as f:
        json.dump(projects, f, indent=2)
    plist = projects if isinstance(projects, list) else projects.get("projects", [])
    if not plist:
        print("No projects/units returned at all - check auth.")
        return

    target_code = sys.argv[2].strip().upper() if len(sys.argv) > 2 else None
    chosen = plist[0]
    if target_code:
        for p in plist:
            code = ((p.get("unit") or {}).get("code") or "").upper()
            if code == target_code:
                chosen = p
                break
        else:
            print("Unit '%s' not found among your enrolled units. Dumping the first one instead."
                  % target_code)

    pid = chosen["id"]
    uid = chosen.get("unit_id") or (chosen.get("unit") or {}).get("id")
    ucode = (chosen.get("unit") or {}).get("code", "unknown")
    with open(os.path.join(HERE, "dump_project_full.json"), "w") as f:
        json.dump(api.project(pid), f, indent=2)
    with open(os.path.join(HERE, "dump_task_definitions.json"), "w") as f:
        json.dump(api.task_definitions(uid), f, indent=2)
    print("Dumped unit %s. Wrote dump_*.json next to the script." % ucode)


def cmd_dashboard(cfg, units=None, tasks=None):
    if units is None:
        units, tasks = gather(cfg)
    out = cfg.get("dashboard", "output_path",
                  fallback=os.path.join(HERE, "index.html"))
    raw = cfg.get("dashboard", "current_units", fallback="").strip()
    current_units = [c.strip() for c in raw.split(",") if c.strip()] or None
    render_dashboard(units, tasks, out, current_units=current_units)
    print("Dashboard written to %s (%d tasks)" % (out, len(tasks)))

    if cfg.has_section("github") and cfg.get("github", "repo_name", fallback="").strip():
        try:
            gh = GitHubAPI(cfg)
            with open(out, "rb") as f:
                html_bytes = f.read()
            pages_path = cfg.get("github", "pages_path", fallback="index.html")
            gh.put_file(pages_path, html_bytes, "Update OnTrack dashboard")
            print("Pushed dashboard to GitHub Pages.")
        except Exception as e:
            print("Could not push to GitHub Pages (dashboard is still saved "
                  "locally, so nothing is lost): %s" % e)


def cmd_remind(cfg, units=None, tasks=None):
    if units is None:
        units, tasks = gather(cfg)
    start_lead = cfg.getint("reminders", "start_lead_days", fallback=0)
    due_lead = cfg.getint("reminders", "due_lead_days", fallback=3)
    digest = build_digest(tasks, start_lead, due_lead)

    # Optional early warning if the token is a readable JWT nearing expiry.
    warn_days = cfg.getint("reminders", "token_warn_days", fallback=3)
    days_left = token_days_remaining(cfg.get("ontrack", "auth_token"))
    notice = None
    if days_left is not None and days_left <= warn_days:
        notice = ("Heads-up: your OnTrack token expires in about %d day(s). "
                  "Refresh it soon (settoken) so reminders keep working." % days_left)

    prefix = cfg.get("email", "subject_prefix", fallback="OnTrack")
    state = load_state()
    today_iso = melbourne_today().isoformat()
    force = cfg.getboolean("reminders", "force", fallback=False)

    if not digest_has_content(digest):
        # Nothing to do today, but still warn about an imminent token expiry.
        if notice and state.get("last_expiry_warn") != today_iso:
            send_email(cfg, prefix + " \u2014 token expiring soon",
                       "<p style='font-family:sans-serif'>%s</p>" % notice, notice)
            state["last_expiry_warn"] = today_iso
            save_state(state)
            print("Sent token-expiry heads-up.")
        else:
            print("Nothing actionable today. No email sent.")
        return

    # Send at most one digest per calendar day.
    if state.get("last_digest_date") == today_iso and not force:
        print("Digest already sent today. Skipping.")
        return

    text, html = render_digest_email(digest, notice=notice)
    n = sum(len(x) for x in digest)
    subject = prefix + " \u2014 %d item(s) need attention" % n
    send_email(cfg, subject, html, text)
    state["last_digest_date"] = today_iso
    if notice:
        state["last_expiry_warn"] = today_iso
    save_state(state)
    print("Digest emailed (%d items)." % n)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    cfg = load_config()
    try:
        if cmd == "test":
            cmd_test(cfg)
        elif cmd == "dump":
            cmd_dump(cfg)
        elif cmd == "settoken":
            cmd_settoken(cfg)
        elif cmd == "dashboard":
            cmd_dashboard(cfg)
        elif cmd == "remind":
            cmd_remind(cfg)
        elif cmd == "all":
            units, tasks = gather(cfg)
            cmd_dashboard(cfg, units, tasks)
            cmd_remind(cfg, units, tasks)
        else:
            sys.exit("Unknown command '%s'. Use: test | dump | settoken | "
                     "dashboard | remind | all" % cmd)
    except HTTPError as e:
        if e.code in (401, 403, 419):
            # Don't silently die on a scheduled run: email an alert so you know
            # to refresh the token. settoken handles its own errors above.
            alerted = False
            if cmd in ("dashboard", "remind", "all", "test"):
                alerted = send_auth_alert(cfg, "Auth failed with HTTP %d." % e.code)
            msg = "\nAuth failed (%d) \u2014 your token has likely expired." % e.code
            msg += ("\nAn alert email was sent to you." if alerted else
                    "\n(No alert email sent: already alerted today, or email not set up.)")
            msg += ('\nRefresh it:  python3 ontrack_companion.py settoken "<new token>"'
                    '   (see README).')
            sys.exit(msg)
        sys.exit("HTTP error %d from OnTrack: %s" % (e.code, e.reason))
    except URLError as e:
        sys.exit("Network error reaching OnTrack: %s" % e.reason)


if __name__ == "__main__":
    main()
