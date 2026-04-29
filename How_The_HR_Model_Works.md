# How the MLB HR Model Picks Its Daily Card

A plain-English walkthrough of how the model decides who's most likely to hit a home run on any given day. Written for friends to read before we sit down and argue about it.

---

## The 30-second version

Every batter in every MLB game today gets a score from 0 to 100 called the **composite**. The composite is a weighted blend of five factors: how much raw power the batter has, how bad a matchup the pitcher is for him, how friendly the ballpark is to his handedness, how hot he's been lately, and what the weather looks like at first pitch. We then bucket batters into three tiers based on their season HR rate, pick the highest composites out of each tier, and build an 8-player card with tier-point multipliers so that longshots are worth more than chalk.

That's the whole thing. Everything below is the detail.

---

## Step 1: Build the daily slate

Before anything can be scored, the model has to know what games are being played today, who's pitching, and who's in the lineup.

- **Schedule and probable pitchers** come from the free MLB Stats API. We get every game on today's date, the starting pitchers, venue, and first-pitch time.
- **Confirmed lineups** come from MLB's "matchup" endpoint (the same source MLB.com uses for its lineup cards). If lineups aren't posted yet, we fall back to projected lineups based on recent starts.
- **Weather** comes from Open-Meteo, a free forecast API. We look up the ballpark's coordinates, convert first-pitch to the stadium's local timezone, and pull the hourly temperature, wind speed, and wind direction.
- **Park factors** come from a curated table stored in our own database. Each park has three numbers: an overall HR factor, a left-handed batter HR factor, and a right-handed batter HR factor. 100 is league average, 130 is Coors, 82 is Oracle Park.
- **Pitcher season stats** (ERA, HR/9, hard-hit percentage allowed, strikeout rate) come from the MLB Stats API and are stored in our database.
- **Pitcher "arsenals"** (average fastball velo, pitch mix, spin rate, release extension) come from Baseball Savant via Statcast pitch-by-pitch data.
- **Every HR the batter has hit over the last 18 months** — pitcher who gave it up, pitch type, velo, spin — is stored in our database. This feeds the archetype matching we'll explain later.

All of this gets pulled once and cached so the scoring runs in seconds.

---

## Step 2: Bucket the batters into tiers

Not every batter is created equal. A season HR rate leader has a very different baseline odds of going deep than a ninth-hole slap hitter. So we split the player pool into three tiers before scoring.

Tiers are built off a **rolling window of the last ~40 games**, ranked by **HR per plate appearance**. We cross the season boundary if we need to — if it's April and a player only has a few games logged, we blend in the tail of last season to get a fair read. Then:

- **Tier 1 (Chalk):** top ~15% of qualified batters. Think Judge, Ohtani, Schwarber — guys whose baseline HR odds are so high they're practically table stakes.
- **Tier 2 (Mid):** next ~30%. Solid power bats. Riskier than T1, bigger payout if they hit.
- **Tier 3 (Longshot):** next ~30%. Guys with real but volatile power. These are the picks that win you the day when they come through.
- Bottom ~25% don't get scored at all. Not eligible for the card.

The point of tiers is that the final card is built with a **tier-points multiplier** (T1 = 1 point, T2 = 3 points, T3 = 9 points), which rewards you for correctly flagging longshots, not just chalk. More on that at the end.

---

## Step 3: Score each batter on five factors

For every batter in the slate, the model computes five sub-scores on a 0-100 scale. Here's each one in plain English.

### Factor 1 — Power Score (30% of the composite)

"How much raw power does this batter have?"

Four inputs, all pulled from season stats:

- **Barrel percentage.** The share of batted balls that are "barreled" — the ideal combination of exit velocity and launch angle that produces HRs. 0% is bad, 25% is elite.
- **Exit velocity.** How hard the batter hits the ball on average. 80 mph is well below league average, 100 mph is elite.
- **HR/FB percentage.** Of all the fly balls this batter hits, what share clear the fence. 0% is bad, 30% is elite.
- **Isolated Slugging (ISO).** Slugging percentage minus batting average — a clean measure of extra-base power. .100 is below average, .350 is elite.

Each of these gets scaled to 0-100 and we take the average.

### Factor 2 — Matchup Score (25% of the composite)

"How vulnerable is today's pitcher, and does his style match the kind of arm this batter feasts on?"

This is the smartest factor in the model. It has two signals blended 50/50.

**Signal A: Pitcher vulnerability.** Is this pitcher generally hittable? We look at his HR/9, ERA, hard-hit percentage allowed, and strikeout rate. An ace like Skubal or Wheeler gets a vulnerability score near 10-15; a back-end starter with a 5.50 ERA gets 70-80.

**Signal B: Archetype similarity.** This is the secret sauce. For every batter, we look at every HR he's hit over the last 18 months and build a "victim profile" — the weighted-average pitcher type he crushes. If a batter mostly homers off 94 mph fastballs from righties with average spin, his victim profile reflects that. We then compare that profile against today's pitcher across seven dimensions: fastball velocity, pitch mix, handedness, spin, extension, and a couple others. The more the pitcher matches the batter's victim profile, the higher the similarity score.

The two signals get averaged. Then:

- A small platoon bonus (+5) if the batter and pitcher have opposite handedness.
- An "ace dampener" — if the pitcher's vulnerability score is below 25 (truly elite), we multiply the whole matchup score by 0.70. Below 40 (good), we multiply by 0.85. This stops the model from getting cute and picking against Verlander just because his arsenal matches a batter's victim profile.

If the archetype data is missing (new pitcher, no HR history for the batter), the model falls back to the simpler v1 version: pitcher HR/9, hard-hit percentage allowed, and batter wOBA vs. pitcher handedness.

### Factor 3 — Park Score (20% of the composite)

"How HR-friendly is this specific ballpark for a batter of this handedness?"

Every park has three numbers in our table: an overall HR park factor, a left-handed batter park factor, and a right-handed batter park factor. The model picks the one that matches the batter's hand (switch-hitters get the average).

Examples of why the splits matter:

- **Yankee Stadium** is HR-friendly overall (115), but the real story is the short right field porch. Lefties get a factor of 128. Righties get 105. A lefty at Yankee Stadium is in one of the five best HR situations in baseball; a righty is barely above average.
- **Fenway Park** rewards righties because of the Green Monster (118), but the deep right field punishes lefties (100). Devers and Yoshida don't actually benefit from Fenway as lefties the way you'd think.
- **PNC Park** has a brutal right field wall (the Clemente Wall, 21 feet high, 320+ feet from home). Lefties get a factor of 88. Righties get 102. The park plays totally differently depending on who's hitting.
- **Oracle Park** in San Francisco is the most extreme lefty-killer in baseball. Triples Alley in right-center is where lefty HRs go to die. Lefties: 72. Righties: 90.
- **Coors Field** boosts both sides by roughly 30% — that's the thin air, nothing to do with geometry.

The raw park factor (between 70 and 130) gets linearly scaled to 0-100.

### Factor 4 — Form Score (15% of the composite)

"Is this batter hot right now?"

Three inputs, all from the last two weeks of games:

- **HRs hit in the last 14 days.** Zero is cold, five-plus is on fire.
- **Barrel percentage in the last 14 days.** Are the batted balls trending harder and at better angles?
- **Exit velocity trend.** Are recent balls coming off the bat harder than the batter's season average? A +5 mph swing is huge; -5 mph is a slump.

Each scaled to 0-100, then averaged.

### Factor 5 — Weather Score (10% of the composite)

"Is the weather helping or hurting HRs today?"

Two inputs, blended 50/50.

**Temperature.** We use a piecewise curve calibrated to real MLB HR/temperature data. Below 50°F, cold dense air kills carry (score of 25 or worse). 68°F is neutral (50). 85°F gives you a real boost (72). 95°F+ is the kind of hot that turns warning track outs into souvenirs (88+).

**Wind.** If the wind is blowing out toward center field (bearings 315° to 45° on a compass), every mph of wind speed adds points to the score. If it's blowing in from center (135° to 225°), every mph of wind speed subtracts points. Crosswinds get a small boost. Calm conditions (<2 mph) are treated as neutral.

Dome games always score 50 (neutral) since the environment is controlled.

---

## Step 4: Combine into the composite

Now we have five sub-scores between 0 and 100. The composite is a weighted average:

- Power: **30%**
- Matchup: **25%**
- Park: **20%**
- Form: **15%**
- Weather: **10%**

These weights are the "default" config. The model supports other configs for experimentation (power_heavy, matchup_heavy, park_heavy, form_heavy, no_weather) but we run default day-to-day.

**Example walkthrough: Bryce Harper (L) at Yankee Stadium vs. a middling righty on a mild sunny day.**

- Power = 82 (he's elite)
- Matchup = 70 (average pitcher + platoon advantage + archetype match)
- Park = 97 (lefty at Yankee is almost as good as Coors)
- Form = 60 (he's been warming up)
- Weather = 55 (mild day, calm wind)
- Composite = 0.30×82 + 0.25×70 + 0.20×97 + 0.15×60 + 0.10×55 = **73.4**

That would put him squarely in the top tier of the day's board.

---

## Step 5: Apply the repeat penalty

One thing the model actively prevents: picking the same guy every single day just because his numbers always look good. We keep a running log of who's been picked in the last 5 days, and **every time a batter was picked in the recent window, we subtract 3 points from his composite**. Pick him three days in a row and he takes a 9-point hit the next day. This pushes the model to explore the board and keeps the card from becoming "Judge + Ohtani + Schwarber" every single morning.

---

## Step 6: Build the 8-pick card

After scoring, we have a "full board" of every batter sorted by composite. The final card has 8 spots, split across tiers. The default split is **(3 T1, 2 T2, 3 T3)** — three chalk picks, two mids, three longshots.

We pick the highest-composite batter from each tier until we hit the quota, with two guardrails:

1. **No duplicate players.** Obviously.
2. **Max 2 picks per game.** We don't stack three guys from the same lineup, because if the game gets rained out or the starter gets scratched, we'd lose too much of the card at once.

The final card gets sorted by composite (best pick first) for display.

---

## Step 7: Why the tier multipliers matter

Each tier is worth different "points" if the pick hits:

- **T1 = 1 point** (chalk, baseline)
- **T2 = 3 points**
- **T3 = 9 points**

An 8-pick card with 3 T1, 2 T2, and 3 T3 has a theoretical max of 3 + 6 + 27 = **36 points**. Chalk rarely pays much; longshots are where the day is won or lost.

This is why we tier in the first place. You don't want the model to just pick the 8 highest composites overall, because that would be 8 T1 chalk guys every day — low ceiling, boring, and it doesn't reward correct longshot calls.

---

## What the model is NOT doing (yet)

A few things we know we're missing. Kanban these if anything jumps out:

- **No platoon splits from actual data.** We know if a batter has the platoon advantage, but we don't use his actual lifetime numbers vs. RHP/LHP. A batter who's .310 vs. righties and .180 vs. lefties should score very differently depending on the opposing starter.
- **No hot/cold lineup context.** If a batter is hitting cleanup behind guys who are getting on base, he gets more PAs with runners on. We don't model that.
- **No bullpen factor.** If the starter only goes 5 innings and the home team has a pitching-to-contact bullpen, the batter might face pitchers who are way more hittable than the starter. We only score against the starter.
- **No stadium-level weather history.** Coors in April plays very differently from Coors in July. Right now we just score temperature generically.
- **No order-of-operations within a game.** A leadoff hitter gets 4.5 PAs on average; a cleanup hitter might get 4.0; a #9 hitter might get 3.5. We don't adjust for expected PAs, which is a real signal.
- **Park factors are a curated seed, not a live feed.** They're roughly the Baseball Savant 3-year rolling averages, but we're not pulling them fresh. The plan is to wire in a live Savant pull so the table stays current.

---

## The one-paragraph recap

For every batter in today's lineup, we score five things: his raw power (30%), how the matchup sets up with today's pitcher including an archetype-similarity model built from his HR history (25%), how HR-friendly the park is for his handedness (20%), how hot he's been in the last two weeks (15%), and what the weather looks like at first pitch (10%). We subtract points if he's been picked a lot lately. We split the player pool into three tiers by season HR rate (Chalk, Mid, Longshot), pick the best composites out of each tier, enforce a max of two picks per game, and build an 8-player card that rewards correctly flagged longshots with 9x the points of chalk. That's the model.
