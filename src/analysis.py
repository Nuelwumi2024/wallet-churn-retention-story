"""Wallet churn & retention analysis -> figures/ and REPORT.md (all numbers computed here).

Run:  python src/gen_data.py --out data/raw && python src/analysis.py
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "figures"
FIG.mkdir(exist_ok=True)
BLUE, RED, GREY, GREEN = "#1f5fbf", "#d1342f", "#9aa3ad", "#2a8a57"
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "font.size": 11,
                     "axes.titleweight": "bold", "axes.titlesize": 13, "figure.dpi": 130})

raw = (ROOT / "data/raw").as_posix()
con = duckdb.connect()
con.execute(f"""
CREATE VIEW cust AS SELECT *, date_trunc('month', CAST(signup_date AS DATE)) AS cohort FROM read_csv_auto('{raw}/customers.csv');
CREATE VIEW tx AS SELECT customer_id, CAST(txn_timestamp AS TIMESTAMP) ts, amount_ngn, txn_type, status
                  FROM read_csv_auto('{raw}/transactions.csv') WHERE status = 'success';
-- customer x calendar-month activity, expressed as months since signup month
CREATE VIEW act AS
  SELECT DISTINCT c.customer_id, c.cohort, c.acquisition_channel, c.kyc_tier,
         date_diff('month', c.cohort, date_trunc('month', t.ts)) AS m
  FROM cust c JOIN tx t USING (customer_id);
""")
q = lambda s: con.execute(s).df()  # noqa: E731

# ---------- 1. cohort retention heatmap (cohorts with >= 6 months of follow-up)
last_month = pd.Timestamp("2025-06-01")
coh = q("""SELECT a.cohort, a.m, count(DISTINCT a.customer_id) active,
                  (SELECT count(*) FROM cust c WHERE c.cohort = a.cohort) size
           FROM act a GROUP BY 1, 2, 4 ORDER BY 1, 2""")
coh["ret"] = coh["active"] / coh["size"]
piv = coh.pivot(index="cohort", columns="m", values="ret")
piv = piv[piv.index <= pd.Timestamp("2024-12-01")].loc[:, 0:12]
for cohort in piv.index:  # mask months that haven't happened yet
    maxm = (last_month.year - cohort.year) * 12 + last_month.month - cohort.month
    piv.loc[cohort, piv.columns > maxm] = np.nan

fig, ax = plt.subplots(figsize=(11, 5.2))
im = ax.imshow(piv.values * 100, cmap="Blues", vmin=0, vmax=100, aspect="auto")
ax.set_xticks(range(piv.shape[1]), [f"M{c}" for c in piv.columns])
ax.set_yticks(range(piv.shape[0]), [d.strftime("%b %Y") for d in piv.index])
for i in range(piv.shape[0]):
    for j in range(piv.shape[1]):
        v = piv.values[i, j]
        if not np.isnan(v):
            ax.text(j, i, f"{v*100:.0f}", ha="center", va="center", fontsize=8, color="white" if v > 0.55 else "#222")
ax.set_title("Monthly cohort retention (% of cohort transacting in month N after signup)")
ax.spines[:].set_visible(False)
fig.colorbar(im, ax=ax, label="% active")
fig.tight_layout(); fig.savefig(FIG / "01_cohort_retention_heatmap.png"); plt.close(fig)

m1 = piv[1].mean(); m3 = piv[3].mean(); m6 = piv[6].mean()

# ---------- 2. retention curves by acquisition channel (who sticks?)
ch = q("""SELECT a.acquisition_channel ch, a.m, count(DISTINCT a.customer_id) active FROM act a
          WHERE a.cohort <= DATE '2024-12-01' AND a.m <= 6 GROUP BY 1, 2""")
size = q("SELECT acquisition_channel ch, count(*) n FROM cust WHERE cohort <= DATE '2024-12-01' GROUP BY 1").set_index("ch")["n"]
ch["ret"] = ch["active"] / ch["ch"].map(size)
fig, ax = plt.subplots(figsize=(8, 4.6))
final = {}
for name, g in ch.groupby("ch"):
    g = g.sort_values("m")
    final[name] = g.loc[g.m == 6, "ret"].iloc[0]
worst, best = min(final, key=final.get), max(final, key=final.get)
for name, g in ch.groupby("ch"):
    g = g.sort_values("m")
    color = RED if name == worst else GREEN if name == best else GREY
    ax.plot(g["m"], g["ret"] * 100, color=color, lw=2.6 if color != GREY else 1.4, marker="o", ms=4)
    ax.text(6.08, g["ret"].iloc[-1] * 100, name, va="center", fontsize=9, color=color)
ax.set_xlim(0, 7.6); ax.set_xlabel("Months since signup"); ax.set_ylabel("% still transacting")
ax.set_title(f"{best} customers stick; {worst} customers leak")
fig.tight_layout(); fig.savefig(FIG / "02_retention_by_channel.png"); plt.close(fig)

# ---------- 3. activation: early behaviour predicts long-term retention
act = q("""WITH early AS (
              SELECT c.customer_id, count(t.ts) AS early_txns
              FROM cust c LEFT JOIN tx t ON t.customer_id = c.customer_id
                   AND t.ts < CAST(c.signup_date AS DATE) + INTERVAL 14 DAY
              WHERE c.cohort <= DATE '2024-12-01' GROUP BY 1),
           later AS (
              SELECT DISTINCT a.customer_id FROM act a WHERE a.m = 4)
           SELECT CASE WHEN early_txns = 0 THEN '0' WHEN early_txns <= 2 THEN '1-2' WHEN early_txns <= 5 THEN '3-5' ELSE '6+' END AS bucket,
                  count(*) n, avg(CASE WHEN l.customer_id IS NOT NULL THEN 1.0 ELSE 0 END) m4_retention
           FROM early e LEFT JOIN later l USING (customer_id) GROUP BY 1""")
act["bucket"] = pd.Categorical(act["bucket"], ["0", "1-2", "3-5", "6+"], ordered=True)
act = act.sort_values("bucket")
fig, ax = plt.subplots(figsize=(7, 4.2))
ax.bar(act["bucket"].astype(str), act["m4_retention"] * 100, color=[GREY, GREY, BLUE, GREEN])
for i, v in enumerate(act["m4_retention"]):
    ax.text(i, v * 100 + 1, f"{v*100:.0f}%", ha="center", fontweight="bold")
ax.set_xlabel("Successful transactions in first 14 days"); ax.set_ylabel("% active in month 4")
ax.set_title("Activation is the lever: early usage predicts retention")
fig.tight_layout(); fig.savefig(FIG / "03_activation_vs_retention.png"); plt.close(fig)
lo, hi = act.iloc[0], act.iloc[-1]

# ---------- 4. value concentration
val = q("""SELECT customer_id, sum(amount_ngn) v FROM tx GROUP BY 1 ORDER BY v DESC""")
top10 = val["v"].head(int(len(val) * 0.1)).sum() / val["v"].sum()

# ---------- 5. churn by KYC tier
kyc = q("""SELECT kyc_tier, count(*) n, avg(CASE WHEN churn_date IS NOT NULL THEN 1.0 ELSE 0 END) churned
           FROM cust GROUP BY 1 ORDER BY 1""")

def row(d, k):
    return d.loc[d.kyc_tier == k, "churned"].iloc[0] * 100

act_tbl = "\n".join(f"| {r.bucket} | {int(r.n):,} | {r.m4_retention*100:.0f}% |" for r in act.itertuples())
report = f"""# Wallet churn & retention - the story

**Business question:** *Why do wallet customers go quiet, and what should Growth do about it?*
**Data:** {int(q('select count(*) c from cust').c[0]):,} customers, signups Jan 2024 - Mar 2025 (synthetic).

## 1. The shape of the problem
Average retention across 2024 cohorts is **{m1*100:.0f}% in month 1, {m3*100:.0f}% in month 3 and {m6*100:.0f}% in month 6**.
![heatmap](figures/01_cohort_retention_heatmap.png)

## 2. Not all acquisition channels are equal
By month 6 the best channel (**{best}**, {final[best]*100:.0f}%) retains **{final[best]/final[worst]:.1f}x** as many customers as the worst (**{worst}**, {final[worst]*100:.0f}%).
![channels](figures/02_retention_by_channel.png)

## 3. Activation is the lever
| Txns in first 14 days | Customers | Active in month 4 |
|---|---|---|
{act_tbl}

Customers with 6+ early transactions are **{hi.m4_retention/max(lo.m4_retention, 1e-9):.1f}x** as likely to be active in month 4 as customers with none.
![activation](figures/03_activation_vs_retention.png)

## 4. Who matters
The top 10% of customers by value drive **{top10*100:.0f}%** of transaction volume. KYC tier 1 customers churn at **{row(kyc,1):.0f}%** vs **{row(kyc,3):.0f}%** for tier 3.

## Recommendations
1. **Re-weight acquisition spend** away from {worst} toward {best}; compare CAC *per retained customer*, not per signup.
2. **Build an activation journey:** nudge every new customer to 3+ transactions in 14 days (airtime top-up, first bill payment, cashback on first POS spend).
3. **Reduce KYC tier-1 friction/limits** with an upgrade prompt - this segment churns fastest.
4. **Protect the top decile** with proactive service and rewards; losing a few of them moves volume more than the long tail.

## Caveats
The activation link is a **correlation** - engaged customers transact early *and* stay - so the recommended nudge should be validated with an A/B test before scaling. Synthetic data with built-in effects; "churn" here = no activity after `churn_date`. In production, define churn as N days of inactivity and run this as a scheduled model (see my dbt repo).
"""
(ROOT / "REPORT.md").write_text(report, encoding="utf-8")
print(report)
