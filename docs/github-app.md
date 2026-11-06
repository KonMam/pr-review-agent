# Posting reviews as a GitHub App

By default the action posts reviews as `github-actions[bot]`, using the
workflow's `GITHUB_TOKEN`. To give the reviewer its own name and icon, the way
CodeRabbit has one, create a GitHub App and pass its credentials to the action.
The action then mints a short-lived installation token for that App with
[`actions/create-github-app-token`](https://github.com/actions/create-github-app-token)
(pinned by SHA in `action.yml`) and posts with it. Reviews show as
`<app-name>[bot]` with the App's avatar.

Everything below is a manual step for the owner: the App, its private key and
the secrets are credentials, so no script or agent creates them.

## 1. Create the App

Go to <https://github.com/settings/apps/new> (under KonMam):

- **Name:** `pr-review`. App names are global on GitHub, so if it's taken use
  something like `pr-review-konmam`. Comments show as `<name>[bot]`.
- **Homepage URL:** `https://github.com/KonMam/pr-review-agent`
- **Webhook:** untick **Active**. The action calls the API itself and needs no
  events.
- **Repository permissions:**
  - Pull requests: **Read and write** (to post the review)
  - Contents: **Read-only**
  - Metadata: **Read-only** (GitHub requires it)
  - Everything else: No access.
- **Where can this GitHub App be installed?** Only on this account.

Click **Create GitHub App**, then on the App's page:

- **Display information → Upload a logo:** use
  [`docs/assets/pr-review-avatar.png`](assets/pr-review-avatar.png) (512×512).
- Note the **Client ID** (it starts with `Iv`).
- **Private keys → Generate a private key.** A `.pem` file downloads. Treat it
  like a password: don't commit it, and delete the local copy after step 3.

## 2. Install it on go-taskqueue only

On the App's page, **Install App → KonMam → Only select repositories →
go-taskqueue → Install**.

## 3. Add the secrets to go-taskqueue

```bash
gh secret set PR_REVIEW_APP_CLIENT_ID --repo KonMam/go-taskqueue --body "<client id>"
```

```bash
gh secret set PR_REVIEW_APP_PRIVATE_KEY --repo KonMam/go-taskqueue < path/to/pr-review.private-key.pem
```

The action takes the Client ID rather than the App ID: `create-github-app-token`
v3 deprecated `app-id` in favour of `client-id`.

## 4. Pass them in the workflow

In go-taskqueue's `.github/workflows/pr-review.yml`:

```yaml
    steps:
      - uses: KonMam/pr-review-agent@main
        with:
          openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
          app-client-id: ${{ secrets.PR_REVIEW_APP_CLIENT_ID }}
          app-private-key: ${{ secrets.PR_REVIEW_APP_PRIVATE_KEY }}
```

Keep `pull-requests: write` in the workflow's `permissions` for now. It's
unused while the App posts, but it keeps the fallback working if the App
secrets are removed.

## How the action chooses a token

- Both `app-client-id` and `app-private-key` set: it mints the App's token,
  scoped to this repository with pull-requests write and contents read, and
  revokes it when the job ends.
- Neither set: it posts with `github-token`, which defaults to the workflow's
  `GITHUB_TOKEN`. This is the old behaviour.
- Only one set: the job fails with an error rather than quietly falling back to
  `github-actions[bot]`.
- A fork PR gets no secrets, so the review is skipped, as before.
