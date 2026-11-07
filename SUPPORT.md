# Support page — how to switch the payment buttons on

The public support page is `docs/donate.html`. It is deliberately **link-based**: it never shows
bank details, and it only shows the providers you actually use.

## 1. Get one or more payment links

| Provider | What it gives the payer | Where to create it |
|---|---|---|
| **Stripe Payment Link** | card, Apple Pay, Google Pay — the closest to "one button, every method" | dashboard.stripe.com → Payment links → New |
| **PayPal Donate** | PayPal balance or card, Google Pay in some regions | paypal.com/donate/buttons → create a donate button |
| **Ko-fi** | one-off or monthly, cards and PayPal | ko-fi.com |
| **GitHub Sponsors** | monthly or one-off, tied to the GitHub account | github.com/sponsors |

One link is enough. Stripe is the usual choice when the payer should be able to use any card or
wallet without a PayPal account.

## 2. Paste it into the page

Open `docs/donate.html`, find the `LINKS` object near the bottom, and fill in what you have:

```js
const LINKS = {
  stripe:  "https://buy.stripe.com/xxxxxxxx",
  paypal:  "",
  kofi:    "",
  sponsor: ""
};
```

Empty entries are simply not rendered. If all are empty, the page shows a single card pointing at
the issue tracker instead of a dead button — that is the current state.

## 3. Publish

Commit and push; GitHub Pages rebuilds in about 30–60 seconds. Verify with:

```bash
curl -s https://sharenganeye.github.io/antiloop-watchdog/donate.html | grep -c "buy.stripe.com"
```

## Notes

- Nothing about the project changes with a donation: same licence, same code, no gated features.
  Keep the wording that way — restrained, no amount suggested, no urgency.
- Bank details must never be committed. Removal from git history needs a force-push, so keep them
  out from the start (this repository was already rewritten once for exactly that reason).
