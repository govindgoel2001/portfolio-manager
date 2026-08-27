# AI Portfolio Manager

A buy and hold investment tool that tells you what to hold and why, in plain
English. It reads public filings to see what insiders, members of Congress and
a list of tracked funds are actually buying, puts four different AI models on
each name to argue it out, and measures everything against the S&P 500.

It runs on a paper account. Nothing reaches a broker without you clicking
approve.

## The thing worth knowing before you start

This app includes a leaderboard that measures 21 well known portfolios against
the index, using their own filings, entered on the date those filings became
public. Here is what it currently reports:

**6 of 21 beat the S&P 500. The median is 0.33 points behind it.**

Berkshire Hathaway, Renaissance, Pershing Square, Tiger Global and Lone Pine
are all behind the index on that measurement. So is the Nancy Pelosi tracker.

That number is the reason this exists. Most investing tools are built to make
you feel clever. This one puts the comparison you would rather not look at on
every screen, permanently.

## What it does

Gives you a diversified basket across index funds, individual companies,
sector funds, themes, gold and energy, and a small capped slice of crypto.
Every holding gets one sentence explaining why it is there. No Sharpe ratios,
no jargon you would have to go and look up.

Lets you import what you already own, by connecting a broker or pasting a
list, and tells you which holdings match, which look oversized, and which it
has no opinion on at all.

Lets you follow somebody else's portfolio, with their measured record next to
their name rather than their reputation. That includes inverse strategies:
buying what Michael Burry sold is measured too, and it has trailed the index
by 4.11 points on average.

Shows a US sector heatmap you can click into, down to which holdings drove a
sector's move and which tracked managers own it.

Runs a committee of four different AI models on each name, eight times a day.
They answer independently, then cross examine each other's factual claims.
That second round earns its keep: on the first live run all four independently
caught a data bug in the fundamentals feed.

## What it will not do

It will not make you money, and it will not promise to. The target in the
config is eight points a year over the S&P, and the app tells you to be
skeptical of that target on the same screen it displays it.

It will not trade for you. Every order needs an approval click, and connected
brokers are read only regardless of what you connect.

It is not financial advice. It is a research tool that shows its working.

## Setup

You need Docker, a machine that stays on, and about twenty minutes. The free
tier of everything below is enough.

### 1. Get the code

```bash
git clone https://github.com/YOUR-USERNAME/ai-portfolio-starter.git
cd ai-portfolio-starter
cp .env.example .env
```

### 2. Fill in the four things that matter

Open `.env`. Everything not listed here has a working default.

**A dashboard password.** This is the only thing between the internet and your
approve button. Generate both of these with `openssl rand -hex 32`.

```bash
PM_DASHBOARD_PASSWORD=paste-a-long-random-string
PM_SECRET_KEY=paste-another-one
```

**Your email, for the SEC.** The SEC requires automated clients to identify
themselves with a real contact address. It goes to the SEC and nowhere else.
Leave it blank and the congressional data still works, but insider filings and
the fund leaderboard get skipped.

```bash
SEC_CONTACT_EMAIL=you@example.com
```

**Alpaca paper keys**, free, from
[alpaca.markets](https://app.alpaca.markets/paper/dashboard/overview). Sign up,
switch to Paper Trading, generate a key pair.

```bash
ALPACA_KEY_ID=PK...
ALPACA_SECRET_KEY=...
```

Paper keys begin `PK` and live keys begin `AK`. If yours begins `CK` you have
copied an OAuth app credential, which is a different thing and will not work.
Without any keys the app still runs end to end on a simulated broker, so you
can look around before signing up for anything.

**A reasoning backend**, so the AI parts work:

```bash
PM_LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Or leave `PM_LLM_BACKEND=none`. Everything runs without it and writes
deterministic explanations instead of model written ones.

### 3. Start it

```bash
cd deploy
docker compose up -d --build
```

The first build takes a few minutes because it bakes in the embedding model.
Then open `http://localhost:8791` and log in.

### 4. Look at these three screens first

**What to hold** is the basket, with a sentence per holding. Read the "what to
expect" panel before the holdings themselves.

**Who beats SPY** is the leaderboard. This is the screen that will change how
you think about copy trading.

**Connect and import** takes what you already own and compares it to the
basket.

### 5. Optional: put it on a server with a real certificate

If you have a VPS, `deploy/deploy.sh` handles it:

```bash
./deploy/deploy.sh root@YOUR-SERVER-IP
```

Set `PM_HOSTNAME=pm.YOUR-SERVER-IP.sslip.io` in `.env` first. sslip.io resolves
any `*.your-ip.sslip.io` to that address, which gets you a real HTTPS
certificate without owning a domain. See `deploy/CADDY-VHOST.md` if you already
run a web server on that box.

## Where the data comes from

Everything is a primary source and none of it costs anything.

House Clerk periodic transaction reports, parsed from the filings themselves,
for congressional trades. SEC Form 4 for insider buying and selling. SEC 13F
filings for institutional holdings. Yahoo Finance for prices and fundamentals.
Alpaca for the paper account and market data.

Every one of those is lagged and the app says by how much. A Form 4 lands
within two days. A congressional report can take 45. A 13F is up to 45 days
after a quarter that had already ended.

## Changing what it holds

`config/universe.yaml` is the hard boundary on what can ever be bought. Nothing
outside it enters the portfolio no matter what any model says. Add a ticker
there and it becomes eligible.

`config/risk.yaml` holds the position limits, the cash floor, the minimum
holding period and the return target.

`config/basket.yaml` decides how the basket splits across asset groups.

`config/trackers.yaml` lists the portfolios on the leaderboard. Every CIK in it
was checked against EDGAR. Do not add one from memory.

## Adding your broker

`docs/ADDING-A-BROKER.md` walks through it. The short version is one new file
in `src/brokers/` implementing the protocol in `base.py`, plus two lines of
YAML. Alpaca, Hyperliquid and Groww already exist for reading holdings.

## Running the tests

```bash
python -m pytest tests/ -q
```

117 of them. Several exist because they caught real bugs during development: an
allocator that silently stopped proposing anything as the universe grew, a
rubric weighting that would have frozen the portfolio during a data outage, and
a smart money score where the age decay cancelled itself out.

## Licence

MIT. Do what you like with it.

## A last word on expectations

A safe buy and hold portfolio and an eight point edge over the index pull
against each other. The core of this basket tracks the market and therefore
cannot beat it. Any edge has to come out of the smaller sector, theme and
commodity sleeves, which is also where any shortfall comes from.

The app shows you where it landed between those two things rather than claiming
both. That is the most useful thing it does.
