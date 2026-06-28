"""Cloud Run Job: run configured BigQuery queries once, check each result
against its condition, and email the recipients when a condition is met.

This is a one-shot script (`python main.py`) — it runs, does its work, and
exits. It is NOT a web server. Everything project-specific lives in
`config.json`, the `SQL/` files, and the secret env vars (GMAIL_ADDRESS /
GMAIL_APP_PASSWORD for email, SLACK_WEBHOOK_URL for Slack). To reuse on another
project, edit those things only.

Alerts can be delivered over email, Slack, or both: each channel has its own
block in `config.json` and is sent only when present and not `"enabled": false`.
"""

import json
import operator
import os
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from html import escape
from pathlib import Path

from google.cloud import bigquery

BASE_DIR = Path(__file__).parent
SQL_DIR = BASE_DIR / "SQL"

# Binary comparison operators: compare row[column] against condition["value"].
OPERATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

# Unary operators for BOOL columns: no `value` needed in the condition.
BOOL_OPERATORS = {
    "is_true": lambda v: v is True,
    "is_false": lambda v: v is False,
}


def load_config():
    with open(BASE_DIR / "config.json", "r") as f:
        return json.load(f)


def render_sql(file_name, dataset):
    """Read a SQL file and substitute the {{dataset}} placeholder."""
    sql = (SQL_DIR / file_name).read_text()
    return sql.replace("{{dataset}}", dataset)


def row_meets_condition(row, condition):
    """True when this row satisfies the query's threshold condition."""
    column = condition["column"]
    if column not in row.keys():
        print(f"Warning: condition column '{column}' not in query results.")
        return False

    op = condition["operator"]
    if op in BOOL_OPERATORS:
        # Unary BOOL check, e.g. {"column": "is_alert", "operator": "is_true"}.
        return BOOL_OPERATORS[op](row[column])
    return OPERATORS[op](row[column], condition["value"])


def build_email_html(triggered):
    """Build a single HTML body containing one table per triggered query.

    `triggered` is a list of (query_name, dashboard_url, rows). The dashboard
    link is rendered under each table so recipients can dig into the data; it is
    omitted when a query has no `dashboard_url` configured.
    """
    sections = []
    for name, dashboard_url, rows in triggered:
        header = "".join(f"<th>{escape(str(c))}</th>" for c in rows[0].keys())
        body = ""
        for row in rows:
            cells = "".join(f"<td>{escape(str(v))}</td>" for v in row.values())
            body += f"<tr>{cells}</tr>"

        link = ""
        if dashboard_url:
            url = escape(dashboard_url, quote=True)
            link = f'<p><a href="{url}">Investigate in the Looker Studio dashboard &rarr;</a></p>'

        sections.append(
            f"<h3>{escape(name)}</h3>"
            f'<table border="1" cellpadding="6" cellspacing="0">'
            f"<tr>{header}</tr>{body}</table>"
            f"{link}"
        )
    return "<html><body>" + "<br>".join(sections) + "</body></html>"


def send_email(email_cfg, html_body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["Subject"] = email_cfg["subject"]
    msg["From"] = sender
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg.set_content("This report requires an HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")

    # timeout=30 so a blocked SMTP port fails fast with an error instead of
    # hanging until the job times out.
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)


def _slack_table(rows):
    """Render rows as a fixed-width monospace table (Slack has no real tables).

    Wrapped in a code block by the caller so the columns stay aligned.
    """
    cols = list(rows[0].keys())
    str_rows = [{c: str(r[c]) for c in cols} for r in rows]
    widths = {c: max(len(str(c)), *(len(sr[c]) for sr in str_rows)) for c in cols}

    def fmt(values):
        return " | ".join(str(v).ljust(widths[c]) for c, v in zip(cols, values))

    lines = [fmt(cols), "-+-".join("-" * widths[c] for c in cols)]
    lines += [fmt(sr[c] for c in cols) for sr in str_rows]
    return "\n".join(lines)


def build_slack_message(slack_cfg, triggered):
    """Build a Slack Block Kit payload: one section per triggered query.

    Each section is the query name, a monospace table of the matching rows, and
    the dashboard link (when configured). The header text comes from
    `slack_cfg["header"]` and falls back to a sensible default.
    """
    header = slack_cfg.get("header", "BigQuery alert: thresholds triggered")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]

    for name, dashboard_url, rows in triggered:
        table = _slack_table(rows)
        # Section text caps at 3000 chars; keep room for the name/link/fences.
        if len(table) > 2800:
            table = table[:2800] + "\n… (truncated)"
        text = f"*{escape_slack(name)}*\n```{table}```"
        if dashboard_url:
            text += f"\n<{dashboard_url}|Investigate in the Looker Studio dashboard →>"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})

    return {"blocks": blocks}


def escape_slack(text):
    """Escape the three characters Slack treats specially in mrkdwn text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_slack(slack_cfg, triggered):
    webhook = os.environ["SLACK_WEBHOOK_URL"]
    payload = json.dumps(build_slack_message(slack_cfg, triggered)).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # timeout=30 mirrors the SMTP path: fail fast instead of hanging the job.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Slack webhook failed ({e.code}): {detail}") from e
    if body != "ok":
        raise RuntimeError(f"Slack webhook returned unexpected response: {body!r}")


def channel_enabled(channel_cfg):
    """A channel is active when its config block exists and isn't disabled.

    Presence alone enables it (so existing email-only configs keep working);
    set `"enabled": false` to keep the block but turn the channel off.
    """
    return bool(channel_cfg) and channel_cfg.get("enabled", True)


def run_pipeline():
    config = load_config()
    client = bigquery.Client(project=config.get("project_id"))
    dataset = config["dataset"]

    print("Starting BigQuery orchestration pipeline...")

    triggered = []  # list of (query_name, dashboard_url, [rows that met condition])
    for i, query in enumerate(config["queries"], start=1):
        condition = query.get("condition")
        if not condition:
            # Conditionless queries never trigger or appear in an email: the
            # email is sent only for genuine alerts. Skip without running.
            print(f"Skipping '{query['name']}': no condition configured.")
            continue

        print(f"Executing step {i} of {len(config['queries'])}: {query['name']}")
        sql = render_sql(query["file"], dataset)
        rows = list(client.query(sql).result())

        # NOTE: the condition is checked against EVERY row the query returns, and
        # every matching row is included in the email. A query should therefore
        # return only the row(s) you actually want to alert on (e.g. the most
        # recent day) -- use ORDER BY ... LIMIT or a WHERE clause in the SQL.
        # Returning a large result set will alert if *any* row matches and email
        # all of them.
        matched = [r for r in rows if row_meets_condition(r, condition)]
        if matched:
            triggered.append((query["name"], query.get("dashboard_url"), matched))

    if not triggered:
        print("No conditions met; no notification sent.")
        return

    # Send to every enabled channel. Email and Slack are independent: a project
    # can run either, both, or (by disabling both) none.
    sent = []
    if channel_enabled(config.get("email")):
        send_email(config["email"], build_email_html(triggered))
        sent.append("email")
    if channel_enabled(config.get("slack")):
        send_slack(config["slack"], triggered)
        sent.append("Slack")

    summary = f"{len(triggered)} query(ies) met their condition"
    if sent:
        print(f"{summary}; notified via {', '.join(sent)}.")
    else:
        print(f"{summary}, but no notification channel is enabled.")


def main():
    try:
        run_pipeline()
    except Exception as e:
        # Print to stderr and exit non-zero so the Cloud Run Job execution is
        # marked as Failed (and shows up in the logs).
        print(f"Pipeline failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()