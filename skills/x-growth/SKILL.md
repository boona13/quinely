---
name: x-growth
description: "X/Twitter growth hacking — post, like, comment, repost, follow via browser automation with duplicate tracking"
triggers:
  - post on x
  - post on twitter
  - tweet
  - post tweet
  - like on x
  - like on twitter
  - like tweet
  - comment on x
  - comment on twitter
  - reply on x
  - reply on twitter
  - reply tweet
  - repost on x
  - repost on twitter
  - retweet
  - follow on x
  - follow on twitter
  - x growth
  - twitter growth
  - social media growth
  - growth hack
  - grow my x
  - grow my twitter
  - engage on x
  - engage on twitter
  - x engagement
  - twitter engagement
tools:
  - browser
  - generate_image
  - x_check_action
  - x_log_action
  - x_action_history
  - x_action_stats
  - memory_search
  - memory_save
  - shell_exec
priority: 10
---

# X/Twitter Growth Hacking

You perform growth actions on X (Twitter) via browser automation: post, like, comment, repost, follow.

## CRITICAL RULES

### 1. Duplicate Prevention: CHECK → ACT → VERIFY → LOG

Every interaction MUST follow this pattern:

1. **CHECK** before acting: call `x_check_action` to verify you haven't already done it
2. **ACT** only if the check says "NOT done"
3. **VERIFY** the action actually completed (see verification rules below)
4. **LOG** only after verification passes: call `x_log_action`

If `x_check_action` returns "ALREADY DONE", **SKIP** and move on.

### 2. MANDATORY: Use `slowly=true` for ALL typing on X

X uses React — the standard `fill()` method sets DOM values without triggering React's internal state updates, which means **the Post button may stay disabled** even though text appears in the box. You MUST use `slowly=true` for all type actions on X to fire real keyboard events.

```
browser(action='type', ref='eComposeRef', text='Your text', slowly=true)
```

**NEVER use `fill` or type without `slowly=true` on X.** This is the #1 cause of posts not submitting.

### 3. MANDATORY: Wait + Verify after EVERY submit click

After clicking any submit button (Post, Reply, Repost), you MUST:

1. Use `wait_after_ms=3000` on the click to give X time to process
2. Take a new snapshot AFTER the click
3. Verify the submission succeeded (see verification criteria below)
4. If verification fails, RETRY (up to 2 retries)

```
browser(action='click', ref='ePostButton', wait_after_ms=3000)
browser(action='snapshot')
// Check: Is the compose box gone? Is there a success indicator?
```

### 4. MANDATORY: Snapshot before EVERY interaction

Refs change on every page load. ALWAYS take a fresh snapshot before clicking or typing.

---

## Verification Criteria

### After posting a tweet:
- The compose dialog/box should be **cleared or closed**
- If using `x.com/compose/post`, the page should redirect or the dialog should dismiss
- If composing from home feed, the compose area should reset to empty placeholder text
- **If the compose box still has your text → the post DID NOT submit. Retry.**

### After replying/commenting:
- The reply input should be **cleared**
- Your reply should appear in the thread below the original tweet
- **If the reply box still has your text → the reply DID NOT submit. Retry.**

### After liking:
- The heart icon should change to **filled/red**
- The button name in the snapshot should change from "Like" to "Liked" or "Unlike"

### After following:
- The button should change from "Follow" to "Following"

### After reposting:
- The repost icon should change color (green)
- The repost count should increment

---

## Check Login Status

Before any action, verify the user is logged in:

```
browser(action='navigate', url='https://x.com/home')
browser(action='snapshot')
```

**If logged in:** The snapshot will show the home feed with tweets, a compose box, and navigation. Proceed with the requested action.

**If NOT logged in:** The snapshot will show "Sign in" buttons or a login form. In this case:
- Tell the user: "You need to be logged into X first. I'll open the login page — please log in manually, then tell me when you're done."
- Navigate to: `browser(action='navigate', url='https://x.com/i/flow/login')`
- Wait for the user to confirm
- Snapshot again to verify, then proceed

**Do NOT try to automate the X login flow** — it has CAPTCHAs and verification steps. The Quinely browser preserves sessions, so login persists.

---

## Actions

### Post a Tweet

1. Navigate to the compose page:
```
browser(action='navigate', url='https://x.com/compose/post')
browser(action='wait', timeout_ms=2000)
browser(action='snapshot')
```

2. Find the compose textbox and type with `slowly=true`:
```
browser(action='click', ref='eComposeRef')
browser(action='type', ref='eComposeRef', text='Your tweet text here', slowly=true)
```

3. Wait for the Post button to become enabled, then snapshot and click:
```
browser(action='wait', timeout_ms=1500)
browser(action='snapshot')
```
Find the Post button in the snapshot. It should now be enabled (not grayed out). Click it:
```
browser(action='click', ref='ePostButton', wait_after_ms=3000)
```

4. **VERIFY** — Take a snapshot and confirm the compose dialog closed:
```
browser(action='snapshot')
```
- If the compose dialog is gone or the text area is empty → SUCCESS
- If the compose dialog still shows your text → RETRY: click the Post button again with `wait_after_ms=3000`
- After 2 failed retries, report the failure to the user

5. **LOG** only after verification passes:
```
x_log_action(action='post', target_type='tweet', target_id='https://x.com/youruser/status/NEW_ID', content='Your tweet text')
```

**Alternatively**, you can compose from the home feed — find the "What is happening?!" text area. Same rules apply: `slowly=true`, wait, verify.

---

### Post a Tweet WITH an Image (Higher Engagement)

Tweets with media get **2-3x more engagement** than text-only tweets. When posting content that would benefit from a visual (announcements, tips, opinions, product showcases), generate an image first.

1. **Generate the image** using the `generate_image` tool:
```
generate_image(prompt='Detailed description of the image you want', style='photorealistic', size='landscape')
```
The tool returns a file path like `~/.ghost/generated_images/20260225_143000-your-image.png`.

2. Navigate to compose:
```
browser(action='navigate', url='https://x.com/compose/post')
browser(action='wait', timeout_ms=2000)
browser(action='snapshot')
```

3. **Attach the image using `paste_image`** (copies to clipboard + Cmd+V — the most reliable method):
```
browser(action='paste_image', file_path='~/.ghost/generated_images/YOUR_IMAGE.png', ref='eComposeRef')
```
This copies the image to macOS clipboard and pastes it into the focused compose box. Wait and verify:
```
browser(action='wait', timeout_ms=3000)
browser(action='snapshot')
```

**Fallback** — If `paste_image` doesn't work, use `upload` to set the hidden file input directly:
```
browser(action='upload', file_path='~/.ghost/generated_images/YOUR_IMAGE.png')
```
This uses Playwright's `set_input_files()` on `input[type="file"]` — bypasses Finder entirely.

4. **Verify the image appears** in the compose area (look for a media preview thumbnail in the snapshot).

**NEVER click the media/photo button** — it opens a native macOS Finder dialog that Quinely cannot see or interact with.

5. Now type the tweet text with `slowly=true`:
```
browser(action='click', ref='eComposeRef')
browser(action='type', ref='eComposeRef', text='Your tweet text here', slowly=true)
```

6. Wait, snapshot, click Post with `wait_after_ms=3000`, then VERIFY as in the standard flow.

**When to generate images:**
- The user explicitly asks for an image with their tweet
- Growth hacking sessions (generate eye-catching visuals for engagement)
- Announcements or tips that benefit from a visual
- When the user asks to "make it stand out" or "boost engagement"

**Image prompt tips for tweets:**
- Be specific: "A modern minimalist infographic about AI trends with blue and white color scheme"
- Match the content: if tweeting about coding, generate a code-themed visual
- Use `size='landscape'` for tweets — landscape images display best in the X feed
- Keep it clean and readable — avoid cluttered compositions

---

### Like a Tweet

**For each tweet you want to like:**

1. **CHECK first:**
```
x_check_action(action='like', target_id='https://x.com/username/status/tweet_id')
```
If "ALREADY DONE", **skip**.

2. Navigate to the tweet:
```
browser(action='navigate', url='https://x.com/username/status/tweet_id')
browser(action='snapshot')
```

3. Check the visual state — if the heart is already filled/red or the button says "Unlike"/"Liked", skip it.

4. Click the like button:
```
browser(action='click', ref='eLikeButton', wait_after_ms=1500)
```

5. **VERIFY** — Snapshot and confirm the heart changed:
```
browser(action='snapshot')
```
Look for "Unlike" or "Liked" in the button name. If still "Like" → retry click.

6. **LOG** only after verified:
```
x_log_action(action='like', target_type='tweet', target_id='https://x.com/username/status/tweet_id')
```

**Bulk liking in a feed:** For each visible tweet:
- Extract the tweet URL from links in the snapshot
- `x_check_action` → skip if done
- Check visual state of heart icon
- Click like → verify → log
- Scroll for more: `browser(action='scroll', direction='down', amount=3)` then snapshot

---

### Comment / Reply to a Tweet

1. **CHECK first:**
```
x_check_action(action='comment', target_id='https://x.com/username/status/tweet_id')
```
If already commented, **skip** unless you have a reason to comment again.

2. Navigate to the tweet:
```
browser(action='navigate', url='https://x.com/username/status/tweet_id')
browser(action='wait', timeout_ms=2000)
browser(action='snapshot')
```

3. Find the reply area. Look for "Post your reply" or a reply textbox:
```
browser(action='click', ref='eReplyRef')
browser(action='type', ref='eReplyRef', text='Your thoughtful reply here', slowly=true)
```

4. Wait, snapshot, then click Reply:
```
browser(action='wait', timeout_ms=1500)
browser(action='snapshot')
browser(action='click', ref='eReplyButton', wait_after_ms=3000)
```

5. **VERIFY** — Snapshot and confirm:
```
browser(action='snapshot')
```
- The reply input should be cleared/empty
- Your reply text should appear in the thread
- **If the reply box still has your text → RETRY the click**

6. **LOG** only after verified:
```
x_log_action(action='comment', target_type='tweet', target_id='https://x.com/username/status/tweet_id', content='Your reply text')
```

**Engagement tips:**
- Be genuine and add value — not just "nice" or "great post"
- Ask questions related to the content
- Share your perspective or a relevant insight
- Keep it concise (1-2 sentences)

---

### Repost (Retweet)

1. **CHECK first:**
```
x_check_action(action='retweet', target_id='https://x.com/username/status/tweet_id')
```
If already retweeted, **skip**.

2. Navigate to the tweet:
```
browser(action='navigate', url='https://x.com/username/status/tweet_id')
browser(action='snapshot')
```

3. Click the repost button and wait for the menu:
```
browser(action='click', ref='eRepostButton', wait_after_ms=1500)
browser(action='snapshot')
```

4. Click "Repost" in the dropdown menu:
```
browser(action='click', ref='eRepostOption', wait_after_ms=2000)
```

5. **VERIFY** — Snapshot and check repost icon changed color:
```
browser(action='snapshot')
```

6. **LOG** after verified:
```
x_log_action(action='retweet', target_type='tweet', target_id='https://x.com/username/status/tweet_id')
```

For a **quote repost**:
```
browser(action='click', ref='eQuoteOption', wait_after_ms=1500)
browser(action='snapshot')
browser(action='type', ref='eQuoteText', text='Your quote comment', slowly=true)
browser(action='wait', timeout_ms=1500)
browser(action='snapshot')
browser(action='click', ref='ePostQuote', wait_after_ms=3000)
browser(action='snapshot')  // VERIFY compose closed
```
Log as `quote_retweet`.

---

### Follow a User

1. **CHECK first:**
```
x_check_action(action='follow', target_id='@username')
```
If already followed, **skip**.

2. Navigate to profile:
```
browser(action='navigate', url='https://x.com/username')
browser(action='snapshot')
```

3. Check visual state — if button says "Following", skip.

4. Click Follow:
```
browser(action='click', ref='eFollowButton', wait_after_ms=2000)
```

5. **VERIFY** — Snapshot and confirm button changed to "Following":
```
browser(action='snapshot')
```
If still "Follow" → retry.

6. **LOG** after verified:
```
x_log_action(action='follow', target_type='user', target_id='@username')
```

---

### Search and Engage

1. Review your session:
```
x_action_stats(hours=24)
x_action_history(hours=4)
```

2. Search:
```
browser(action='navigate', url='https://x.com/search?q=your+topic&src=typed_query&f=live')
browser(action='wait', timeout_ms=2000)
browser(action='snapshot')
```

3. For each tweet in results, follow CHECK → ACT → VERIFY → LOG.

4. Use `f=live` for latest, `f=top` for top tweets.

---

## Retry Protocol

If verification fails after clicking a submit button:

1. **First retry:** Take a new snapshot, find the button again (refs may have changed), click with `wait_after_ms=3000`, verify again.
2. **Second retry:** Same as above.
3. **After 2 failed retries:** Stop and report to the user. Something is wrong (rate limit, UI change, session expired).

Common failure reasons:
- **Post button disabled** → You typed with `fill()` instead of `slowly=true`. Clear the field and retype with `slowly=true`.
- **Button ref changed** → Always take a fresh snapshot before retrying.
- **Rate limited** → X may show a rate limit message. Stop and tell the user.
- **Session expired** → The snapshot shows a login screen. Tell the user to re-login.

---

## Growth Strategy Tips

1. **Content posting**: 3-5 tweets per day with relevant hashtags
2. **Engagement**: Like 20-30, reply to 10-15, repost 3-5 per session
3. **Target audience**: Search relevant keywords/hashtags, engage with those communities
4. **Consistency**: Regular posting times
5. **Quality over spam**: Thoughtful replies, not generic ones
6. **Follow strategy**: Follow active users in your niche

## Key Rules Summary

- **CHECK → ACT → VERIFY → LOG** — no exceptions
- **`slowly=true`** for ALL typing on X — no exceptions
- **`wait_after_ms=3000`** for ALL submit button clicks — no exceptions
- **Snapshot before and after** every interaction — refs change on every page load
- **Never log success without verifying** the action actually completed
- **Retry up to 2 times** before reporting failure
- **Start sessions with `x_action_stats`** to know what's done
- **Don't be spammy** — pace interactions naturally
- **If rate-limited or blocked**: stop and tell the user
