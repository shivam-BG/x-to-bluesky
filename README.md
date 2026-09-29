# x-to-bluesky

Automatically copies new posts from X (Twitter) to Bluesky. Runs on GitHub Actions every 10 minutes, no server needed.

## What gets crossposted

| X post type | On Bluesky |
| --- | --- |
| Text post | Same text, with clickable links and hashtags |
| Long post (over 300 characters) | Split into a thread |
| Images (up to 4) | Same images with alt text, auto-compressed if too large |
| Video / GIF | Uploaded as video (falls back to a link to the original if it fails) |
| Self-reply thread | Threaded reply chain |
| Quote tweet of your own crossposted post | Real Bluesky quote post |
| Quote tweet of anything else | Your text plus "Quoting @user" and a link to the original |
| Replies to other people | Skipped |
| Retweets | Skipped (set `CROSSPOST_RETWEETS=1` to include them) |

Edits and deletions on X are not synced.

## Files

- `crosspost.py`: the crossposter
- `requirements.txt`: Python dependencies
- `.github/workflows/crosspost.yml`: the schedule (every 10 minutes)
- `state.json`: created automatically; remembers which tweets have been handled. Don't delete it, or the script will treat the next run as a first run.

## Setup

1. **X API**: create an app in the X Developer Console, add credits (the API is pay-per-use), and copy the **Bearer token**.
2. **Bluesky**: go to Settings, Privacy and security, App passwords, and create one. Never use your main password.
3. **Add repository secrets** under Settings, Secrets and variables, Actions, New repository secret:
   - `X_USERNAME` (your X handle, without the @)
   - `BSKY_HANDLE` (your full Bluesky handle)
   - `X_BEARER_TOKEN`
   - `BSKY_APP_PASSWORD`
4. **First run**: open the Actions tab, choose **crosspost**, click **Run workflow**. The first run only records your latest post and posts nothing, so your history isn't copied over. The log should say "Initialised."
5. From then on it runs by itself. New posts usually appear on Bluesky within 10 to 20 minutes.

## Manual runs

In the Actions tab, **Run workflow** has an arguments box:

- `--dry-run`: print what would be posted without posting or saving anything
- `--backfill 3`: on the very first run only, also post your 3 most recent tweets

## Settings

Your handles live in the repository secrets above, so they never appear in the code. To also crosspost retweets, add this under `env:` in the "Crosspost" step of the workflow file:

```yaml
CROSSPOST_RETWEETS: "1"
```

## Troubleshooting

- **Run failed**: GitHub emails you. Open the run in the Actions tab and read the log. Common causes are out-of-credit X API, an expired or wrong token, or a wrong Bluesky app password.
- **Nothing posted**: check the log for `skip` lines, which say why a tweet was skipped (for example, a reply to someone else).
- **A tweet keeps failing**: it's retried up to 3 times, then skipped so it doesn't block newer tweets.
- **Scheduled runs stopped**: GitHub pauses schedules on repos with no activity for 60 days. The workflow makes a small commit automatically to prevent this, but if it ever happens, click **Enable workflow** in the Actions tab.
- **Want to reset**: delete `state.json` and run again. It will re-initialise and post nothing from before that point.

## Cost

GitHub Actions is free for public repos. The X API charges per post read, so cost scales with how often you post. Check usage in the X Developer Console.
