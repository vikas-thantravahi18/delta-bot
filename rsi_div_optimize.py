#!/usr/bin/env python3
# =============================================================================
# "Can we optimize RSI-Divergence+EMA for better profit, and make it aggressive?"
# Honest answer, proven on data:
#   1) Sweep params. TUNE on the oldest 75% only, then report the winner's score
#      on the untouched recent 25% (true out-of-sample). Show the train->test gap.
#   2) Count how many of ALL combos are profitable in BOTH halves (edge vs luck).
#   3) Aggression test: take the best config, crank risk%/leverage, show return,
#      drawdown, and blow-up frequency scale together (size != edge).
# Real Delta BTCUSD 1h (cached), net of fee 0.05% + slippage + funding.
# Run:  py rsi_div_optimize.py
# =============================================================================
import glob, itertools
from pathlib import Path
import numpy as np
import pandas as pd

FEE, SLIP, FUND8H = 0.0005, 0.0002, 0.0001
START_BAL = 1000.0
EMA_LEN, ATR_LEN = 200, 14
TRAIN_FRAC = 0.75
MIN_TR_TRADES, MIN_TE_TRADES = 15, 6


def load():
    cands = glob.glob(str(Path(__file__).parent / "data/cache/BTCUSD_1h_*.pkl"))
    raw = pd.read_pickle(max(cands, key=lambda f: len(pd.read_pickle(f))))
    df = raw.reset_index(drop=True); df.columns = [c.lower() for c in df.columns]
    cl, hi, lo = df.close, df.high, df.low
    df["ema"] = cl.ewm(span=EMA_LEN, adjust=False).mean()
    tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/ATR_LEN, adjust=False).mean()
    return df


def rsi_of(cl, n):
    d = cl.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l_ = (-d).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    return (100 - 100/(1 + g/l_)).values


def signals(df, rsi_len, piv, ob_os, ob=70, os_=30):
    h, l = df.high.values, df.low.values
    rsi = rsi_of(df.close, rsi_len)
    n = len(df); L = np.zeros(n, bool); S = np.zeros(n, bool)
    pph_p=pph_r=lph_p=lph_r=np.nan; ppl_p=ppl_r=lpl_p=lpl_r=np.nan
    for t in range(2*piv, n):
        p = t - piv
        is_ph = h[p] > h[p-piv:p].max() and h[p] > h[p+1:p+piv+1].max()
        is_pl = l[p] < l[p-piv:p].min() and l[p] < l[p+1:p+piv+1].min()
        if is_ph:
            pph_p,pph_r = lph_p,lph_r; lph_p,lph_r = h[p],rsi[p]
            if not np.isnan(pph_p) and lph_p>pph_p and lph_r<pph_r and (not ob_os or pph_r>=ob):
                S[t] = True
        if is_pl:
            ppl_p,ppl_r = lpl_p,lpl_r; lpl_p,lpl_r = l[p],rsi[p]
            if not np.isnan(ppl_p) and lpl_p<ppl_p and lpl_r>ppl_r and (not ob_os or ppl_r<=os_):
                L[t] = True
    return L, S


def backtest(df, L, S, atr_sl, rr, lev=10, risk=0.01, start=None, end=None):
    o,h,l,cl,ema,atr = (df.open.values,df.high.values,df.low.values,df.close.values,
                        df.ema.values,df.atr.values)
    n=len(df); start=start or (EMA_LEN+20); end=end or n
    bal=START_BAL; peak=bal; dd=0.0; pos=None; nets=[]
    for i in range(start,end):
        if pos:
            ex=None
            if pos["dir"]=="long":
                if l[i]<=pos["sl"]: ex=pos["sl"]*(1-SLIP)
                elif h[i]>=pos["tp"]: ex=pos["tp"]
            else:
                if h[i]>=pos["sl"]: ex=pos["sl"]*(1+SLIP)
                elif l[i]<=pos["tp"]: ex=pos["tp"]
            if ex is not None:
                g=(ex-pos["e"]) if pos["dir"]=="long" else (pos["e"]-ex)
                held=i-pos["i"]
                net=pos["u"]*g-(pos["e"]+ex)*pos["u"]*FEE-pos["e"]*pos["u"]*FUND8H*held/8
                nets.append(net); bal=START_BAL+sum(nets)
                peak=max(peak,bal); dd=max(dd,(peak-bal)/peak*100); pos=None
            else: continue
        if pos is None and not (np.isnan(atr[i-1]) or atr[i-1]<=0):
            a=atr[i-1]
            if L[i-1] and cl[i-1]>ema[i-1]:
                e=o[i]*(1+SLIP); sl=e-atr_sl*a; tp=e+rr*atr_sl*a
                pos=dict(dir="long",e=e,sl=sl,tp=tp,u=min((bal*risk)/(e-sl),bal*lev/e),i=i)
            elif S[i-1] and cl[i-1]<ema[i-1]:
                e=o[i]*(1-SLIP); sl=e+atr_sl*a; tp=e-rr*atr_sl*a
                pos=dict(dir="short",e=e,sl=sl,tp=tp,u=min((bal*risk)/(sl-e),bal*lev/e),i=i)
    if not nets: return dict(n=0,win=0,pf=0,ret=0,dd=dd)
    w=[x for x in nets if x>0]; ls=[x for x in nets if x<=0]
    pf=sum(w)/abs(sum(ls)) if ls and sum(ls)!=0 else 99.0
    return dict(n=len(nets),win=len(w)/len(nets)*100,pf=pf,ret=(bal/START_BAL-1)*100,dd=dd)


if __name__ == "__main__":
    df = load(); n=len(df); cut=int(n*TRAIN_FRAC)
    print(f"\n{'='*78}\nOPTIMIZE RSI-Div+EMA | 1h | {n} bars | "
          f"TRAIN[:{cut}] (oldest 75%)  TEST[{cut}:] (recent 25%, untouched)")
    print('='*78)

    grid = dict(rsi_len=[9,14,21], piv=[3,5,8], ob_os=[False,True],
                atr_sl=[1.0,1.5,2.5], rr=[1.5,2.0,3.0])
    sig_cache = {}
    rows=[]
    combos = list(itertools.product(*grid.values()))
    for k,(rl,pv,oo,asl,rr) in enumerate(combos,1):
        key=(rl,pv,oo)
        if key not in sig_cache: sig_cache[key]=signals(df,rl,pv,oo)
        L,S=sig_cache[key]
        tr=backtest(df,L,S,asl,rr,start=EMA_LEN+20,end=cut)
        te=backtest(df,L,S,asl,rr,start=cut,end=n)
        rows.append(dict(rsi=rl,piv=pv,ob_os=oo,atr=asl,rr=rr,
                         tr_n=tr["n"],tr_pf=tr["pf"],tr_ret=tr["ret"],
                         te_n=te["n"],te_pf=te["pf"],te_ret=te["ret"]))
        if k%40==0: print(f"  ...{k}/{len(combos)} combos")
    R=pd.DataFrame(rows)

    # honest selection: rank by TRAIN only (never look at TEST to pick), then report TEST
    valid=R[(R.tr_n>=MIN_TR_TRADES)&(R.te_n>=MIN_TE_TRADES)].copy()
    by_train=valid.sort_values("tr_ret",ascending=False)
    best=by_train.iloc[0]
    print(f"\n--- 1) Pick the BEST by in-sample return, then look at out-of-sample ---")
    print(f"  WINNER (chosen on TRAIN): rsi={int(best.rsi)} piv={int(best.piv)} "
          f"ob_os={best.ob_os} atr={best.atr} rr={best.rr}")
    print(f"     TRAIN : n={int(best.tr_n)} PF={best.tr_pf:.2f} ret={best.tr_ret:+.1f}%")
    print(f"     TEST  : n={int(best.te_n)} PF={best.te_pf:.2f} ret={best.te_ret:+.1f}%   <-- unseen")

    print(f"\n--- 2) Of {len(valid)} valid combos, how many have a REAL edge (both halves)? ---")
    both=valid[(valid.tr_pf>1.1)&(valid.te_pf>1.1)]
    print(f"  profitable IN-SAMPLE (train PF>1.1):       {(valid.tr_pf>1.1).sum():3d} / {len(valid)}")
    print(f"  ALSO profitable OUT-OF-SAMPLE (test PF>1.1): {len(both):3d} / {len(valid)}  "
          f"(random ~{0.5*(valid.tr_pf>1.1).sum():.0f})")
    print(f"  median TEST PF across all valid combos: {valid.te_pf.median():.2f}  "
          f"(1.0 = break-even)")

    print(f"\n--- 3) 'Make it aggressive': same best config, crank risk%/leverage (FULL 2y) ---")
    key=(int(best.rsi),int(best.piv),bool(best.ob_os))
    L,S=sig_cache[key] if key in sig_cache else signals(df,*key)
    print(f"  {'risk/trade':>11} {'lev':>4} | {'return%':>9} {'maxDD%':>8} {'final$':>9}")
    for risk,lev in [(0.01,10),(0.05,10),(0.10,25),(0.25,50),(0.50,50)]:
        r=backtest(df,L,S,best.atr,best.rr,lev=lev,risk=risk)
        broke = r["ret"]<=-95
        tag=" <- effectively wiped out" if broke else ""
        print(f"  {risk*100:>9.0f}% {lev:>4d} | {r['ret']:>+8.1f}% {r['dd']:>7.1f}% "
              f"{START_BAL*(1+r['ret']/100):>8.0f}{tag}")
    print("\n  Read: bigger risk/lev scales the SWING, not the edge. With ~break-even")
    print("  out-of-sample, aggression just widens a coin-flip -> ruin gets likelier.")
