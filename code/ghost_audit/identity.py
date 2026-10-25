"""Resolve a request email to a user and reconstruct current + historical identifiers."""
from collections import defaultdict

from . import norm


class Subject:
    def __init__(self, user_id, name, keys, emails, sources):
        self.user_id = user_id
        self.name = name
        self.keys = keys            # every linkable identifier (normalized)
        self.emails = emails
        self.sources = sources      # identifier -> audit_id / 'identity_snapshot'

    def __repr__(self):
        return f"Subject({self.user_id}, emails={sorted(self.emails)})"


class IdentityResolver:
    def __init__(self, dataset):
        self.users = {u["user_id"]: u for u in dataset.identities}
        self.history = defaultdict(list)
        for a in dataset.audit_log:
            self.history[a["user_id"]].append(a)
        for events in self.history.values():
            events.sort(key=lambda a: a["event_time"])
        # email -> list of (user_id, valid_from, valid_to)
        self.email_owners = defaultdict(list)
        for uid, u in self.users.items():
            changes = [a for a in self.history[uid] if a["field_name"] == "email"]
            start = ""
            for a in changes:
                self.email_owners[norm.email(a["old_value"])].append((uid, start, a["event_time"]))
                start = a["event_time"]
            self.email_owners[norm.email(u["email"])].append((uid, start, "9999"))

    def resolve(self, request_email, at_time):
        """Return the Subject who owned request_email at at_time (falls back to any owner)."""
        owners = self.email_owners.get(norm.email(request_email), [])
        if not owners:
            return None
        live = [o for o in owners if o[1] <= at_time < o[2]]
        uid = (live or sorted(owners, key=lambda o: o[2], reverse=True))[0][0]
        return self.subject(uid)

    def subject(self, uid):
        u = self.users[uid]
        keys, emails, sources = set(), set(), {}

        def add(value, source, kind):
            if norm.is_null(value):
                return
            if kind == "email":
                e = norm.email(value)
                emails.add(e)
                for k in (e, norm.email_hash(e)):
                    keys.add(k)
                    sources.setdefault(k, source)
            elif kind == "phone":
                p = norm.phone(value)
                if p:
                    keys.add(p)
                    sources.setdefault(p, source)
            elif kind == "email_hash":
                keys.add(value.strip().lower())
                sources.setdefault(value.strip().lower(), source)

        keys.add(uid)
        sources[uid] = "identity_snapshot"
        add(u["email"], "identity_snapshot", "email")
        add(u["phone"], "identity_snapshot", "phone")
        add(u["email_hash"], "identity_snapshot", "email_hash")
        for a in self.history[uid]:
            if a["field_name"] in ("email", "phone", "email_hash"):
                add(a["old_value"], a["audit_id"], a["field_name"])
                add(a["new_value"], a["audit_id"], a["field_name"])
        return Subject(uid, u["name"], keys, emails, sources)
