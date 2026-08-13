# PerKan (Simple Flask Kanban)

A minimal web-based Kanban board powered by Flask and a single JSON data file.

> This is a fork of [sweenig/perkan](https://github.com/sweenig/perkan) with two-way Google
> Calendar sync added — see the [Google Calendar Sync](#google-calendar-sync) section below.
> All credit for the original board/project/list functionality goes to Stuart Weenig.

## Python

1. python -m venv venv
2. venv\Scripts\activate (Windows) or source venv/bin/activate
3. pip install --break-system-packages -r requirements.txt
4. python app.py

Open http://127.0.0.1:5000

## Docker

- Run with the local `data` directory (recommended — run from the `perkan` folder):

`docker run -p 5000:5000 -v "$(pwd)/data:/app/data" perkan`

- Docker Compose (recommended):

From the `perkan` directory:

`docker compose up --build -d`

The service runs Gunicorn with conservative settings (2 workers, 4 threads, 60s timeout).

## Users & Login

PerKan now sits behind a login. The first time you start the app with no accounts yet, the
`/login` page becomes a "create your account" form instead — whoever fills that in becomes the
first user. After that, everyone needs a username/password to reach the board.

There's no admin/role concept: any logged-in user can add, rename, or delete any other user from
**Settings → 👤 Users** (mirroring how Projects/Statuses work — trusted-small-team style, not
locked down). The one restriction is that you can only change your **own** password, and you
can't delete the account you're currently logged in as (log in as someone else first).

Local accounts live in `data/users.json` with hashed passwords (already gitignored). Deleting a
user unassigns their cards and disconnects their Google Calendar; it doesn't delete their cards.

**Sign in with Google:** if `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are configured (see below),
the login page also shows a "Sign in with Google" button — one OAuth consent screen doing double
duty as both login *and* calendar connection. The first time someone signs in that way, PerKan
auto-creates a local account for them (matched by their Google account's stable id, not email, so
it keeps working if their email address ever changes) and connects their calendar in the same
step. Signing in with Google as the very first user bootstraps the board, same as filling out the
manual form. A Google-linked account has no local password by default, so it can only log in via
Google — until that person sets one themselves from **Settings → 👤 Users** (only they can set
their own password). Conversely, connecting your calendar the ordinary way (already logged in
locally, via Settings → 📅 Calendar) also links your account to that Google identity, so "Sign in
with Google" starts working for you too from then on.

## Google Calendar Sync

Each user connects **their own** Google account (personal Gmail or Google Workspace — both use
the identical OAuth flow, no special-casing needed) from **Settings → 📅 Calendar** while logged
in as themselves. There's no single board-wide calendar anymore.

**What it does:**

- Cards have a free-text "Assignee" field (unchanged, just a label) and a separate **"Linked
  user"** dropdown, blank/unassigned by default. The linked user is what determines calendar
  sync — give a card a due date *and* a linked user who's connected their calendar, and it's
  pushed to that person's Google Calendar as an event (30 minutes long, or all-day if you check
  "All day"). Unassigned cards, or cards linked to someone who hasn't connected, just don't sync.
- Reassigning a card's linked user moves its event off the old person's calendar and onto the
  new one's.
- Editing or deleting the card updates or deletes the linked event. A background job polls each
  connected user's calendar roughly once a minute (configurable) and pulls changes back: edits
  to an event's title/description/time are copied onto the linked card, and events created
  directly in someone's Google Calendar are imported as new cards linked to them.
- If an event is deleted on the Google side, PerKan **unlinks** it from the card rather than
  deleting the card — a task disappearing because someone declined or deleted a calendar
  invite would be a bad surprise. Deleting the card in PerKan does delete the calendar event.
- Conflicts (edited in both places since the last sync) are resolved last-write-wins, compared
  by timestamp.
- Connecting your calendar retroactively pushes any cards already linked to you that have a due
  date but never synced (e.g. someone assigned you a card before you connected).

**Setup (one-time, by whoever runs the server):**

You don't need a company/Workspace account for any of this — a free personal Google account is
enough to create the OAuth app that the rest of your group signs into.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and sign in with any Google
   account (your personal Gmail is fine). If this is your first time here, accept the terms and
   create an organization-less project when prompted.
2. **Create a project:** top-left project dropdown → **New Project** → give it any name (e.g.
   "PerKan") → **Create**. Make sure it's selected in that same dropdown afterward.
3. **Enable the Calendar API:** search bar at the top → "Google Calendar API" → open it →
   **Enable**.
4. **Configure the OAuth consent screen** (APIs & Services → OAuth consent screen):
   - User Type: **External** (this is the only option available without a Workspace account —
     that's fine, see the note below).
   - Fill in the required fields: app name (e.g. "PerKan"), your own email as support email and
     developer contact.
   - Scopes: you can skip adding scopes here — PerKan requests them directly at sign-in time.
   - **Test users:** add the Google account email of everyone who should be able to sign in
     (yourself included). This is the step that actually controls who can log into your PerKan —
     without being added here, a personal Gmail account will be rejected by Google before it
     ever reaches PerKan.
   - Leave **Publishing status** as **Testing**. Don't click Publish — see note below.
5. **Create credentials** (APIs & Services → Credentials → **Create Credentials** → **OAuth
   client ID**):
   - Application type: **Web application**.
   - Add an authorized redirect URI matching `GOOGLE_REDIRECT_URI` below (default
     `http://127.0.0.1:5000/auth/google/callback`).
   - Save the generated **Client ID** and **Client Secret** — you'll need them in step 6.

   This is shared infrastructure — one OAuth app serves every PerKan user, each of whom grants
   it access to their own calendar individually.

   > **Why "Testing" instead of "Publish App"?** PerKan asks for calendar access, which Google
   > treats as a sensitive scope. Publishing an app that requests it triggers Google's formal
   > verification process (privacy policy, domain ownership, a demo video, possibly a security
   > review) — built for real companies shipping public products, not a kanban board for a few
   > friends or family. Staying in **Testing** skips all of that: anyone you've added as a test
   > user (up to 100 people) can sign in immediately. The one tradeoff is that Google expires
   > refresh tokens issued by an unverified app after **7 days**, so each connected person will
   > occasionally need to revisit Settings → 📅 Calendar → Connect Google Calendar (or sign in
   > with Google again) to reconnect. For a small trusted group this is a minor, occasional
   > click — not worth going through verification for.
6. Set these environment variables (e.g. in a `.env` file, which is already gitignored):

   | Variable | Default | Purpose |
   |---|---|---|
   | `GOOGLE_CLIENT_ID` | *(required)* | OAuth client ID |
   | `GOOGLE_CLIENT_SECRET` | *(required)* | OAuth client secret |
   | `GOOGLE_REDIRECT_URI` | `http://127.0.0.1:5000/auth/google/callback` | Must match the Cloud Console redirect URI exactly |
   | `GOOGLE_CALENDAR_ID` | `primary` | Which calendar to sync — "primary" resolves to whichever account is connected, so this one setting works for everyone's own primary calendar |
   | `GCAL_TIMEZONE` | `UTC` | IANA timezone (e.g. `America/New_York`) used to interpret card due-date/times. Set this to your actual timezone or events will show at the wrong time. |
   | `GCAL_SYNC_INTERVAL_SECONDS` | `60` | How often to poll for calendar-side changes, per connected user |
   | `GCAL_IMPORT_NEW_EVENTS` | `true` | Whether events created directly in someone's Google Calendar become new cards |
   | `GCAL_DEFAULT_COLUMN` | `todo` | Column newly-imported events land in |
   | `GCAL_SYNC_WINDOW_DAYS` | `90` | How far ahead to look for events on the first (non-incremental) sync |
   | `SECRET_KEY` | *(auto-generated, persisted to `data/secret_key.txt`)* | Flask session signing key. Only set this yourself if you want to invalidate all sessions on demand or share one key across a fresh deployment. |

7. Each person then logs into PerKan and opens **Settings → 📅 Calendar → Connect Google
   Calendar** individually — or, from the login page, just clicks **Sign in with Google**, which
   does the login and calendar connection in one step (see [Users & Login](#users--login)
   above). Either way, only the test users you added in step 4 will be able to complete this.

**Note on multiple Gunicorn workers:** each worker runs its own polling thread, one pass per
connected user per tick. They throttle themselves against duplicate work per-user via each
user's token file, but if you want a strictly single poller, run with `--workers 1` when
Calendar sync is enabled.

**Upgrading from the single-board calendar connection:** if you used the older board-wide
connection, delete `data/gcal_token.json` (now unused) and have each user connect individually
via Settings.