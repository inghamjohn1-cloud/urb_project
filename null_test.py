"""Does POC-migration-x3 add anything over a randomly chosen reference bar?

Same triggers, same filters, same management - only the reference-bar SELECTION
is replaced by a random draw matched to the real base rate. If the real result
sits inside this null distribution, the migration condition is decoration.
"""
import random, statistics as st, sys
import vp195 as V, vp195_spec as S

SPECS=[l.strip() for l in open("samples/cohort.txt") if l.strip() and not l.startswith("#")]

def run(pick_long, pick_short, seed=None):
    rng=random.Random(seed); out=[]
    for spec in SPECS:
        path,_,bk=spec.rpartition(":")
        V.BUCKET=float(bk); bars=V.load(path)
        blackout=S.blackout_opens(bars); busy=-1
        for i in range(21,len(bars)-1):
            if i<=busy: continue
            for long in (True,False):
                sel = pick_long if long else pick_short
                if not sel(bars,i,rng,long): continue
                ref=bars[i]; vaw=ref.vah-ref.val
                if vaw<=0: continue
                if S.climax(bars,i) or ref.key in blackout: continue
                for k in range(i+1,min(i+1+S.TRIGGER_WINDOW,len(bars))):
                    b=bars[k]; hit=None
                    if long:
                        if b.c>ref.vah: hit="c"
                        elif b.l<=ref.val and b.c>=ref.val: hit="p"
                        elif b.l<=ref.poc and b.c>=ref.poc: hit="p"
                    else:
                        if b.c<ref.val: hit="c"
                        elif b.h>=ref.vah and b.c<=ref.vah: hit="p"
                        elif b.h>=ref.poc and b.c<=ref.poc: hit="p"
                    if not hit: continue
                    entry=b.c
                    stop=(min(ref.val,ref.l,b.l)-S.TICK) if long else (max(ref.vah,ref.h,b.h)+S.TICK)
                    if abs(entry-ref.poc)>S.CHASE_WIDTHS*vaw: break
                    if abs(entry-stop)>S.MAX_STOP_WIDTHS*vaw: break
                    t=S.Trade("",1 if long else -1,i,k,entry,stop,vaw,ref)
                    t.trigger=hit; S.manage(bars,t)
                    out.append((1 if long else -1, t.r))
                    busy=t.exit_i if t.exit_i is not None else k
                    break
                break
    return out

real_long  = lambda bars,i,rng,long: S.migration_run(bars,i,True)
real_short = lambda bars,i,rng,long: S.migration_run(bars,i,False)
none_sel   = lambda bars,i,rng,long: False

real=run(real_long,real_short)
rl=[r for s,r in real if s>0]; rs=[r for s,r in real if s<0]
print("REAL   long n=%d %+.3fR | short n=%d %+.3fR | all n=%d %+.3fR"
      %(len(rl),st.mean(rl),len(rs),st.mean(rs),len(real),st.mean(r for _,r in real)))

# base rate of the real condition, to match the random draw
tot=0; upn=0; dnn=0
for spec in SPECS:
    path,_,bk=spec.rpartition(":"); V.BUCKET=float(bk); bars=V.load(path)
    for i in range(21,len(bars)-1):
        tot+=1; upn+=S.migration_run(bars,i,True); dnn+=S.migration_run(bars,i,False)
pu,pd=upn/tot,dnn/tot
print("base rate: up-x3 %.1f%%  down-x3 %.1f%%  (of %d bars)\n"%(100*pu,100*pd,tot))

N=200
means_l=[];means_s=[];means_a=[]
for s in range(N):
    o=run(lambda b,i,rng,l: rng.random()<pu, lambda b,i,rng,l: rng.random()<pd, seed=1000+s)
    L=[r for sd,r in o if sd>0]; Sh=[r for sd,r in o if sd<0]
    if L: means_l.append(st.mean(L))
    if Sh: means_s.append(st.mean(Sh))
    if o: means_a.append(st.mean(r for _,r in o))

def report(name,real_val,dist):
    dist=sorted(dist); n=len(dist)
    pct=100.0*sum(1 for d in dist if d<real_val)/n
    print("%-6s real %+.3fR | random-reference-bar null: mean %+.3fR, "
          "5th %+.3fR, 95th %+.3fR  -> real sits at the %.0fth percentile"
          %(name,real_val,st.mean(dist),dist[int(.05*n)],dist[int(.95*n)],pct))

report("long", st.mean(rl), means_l)
report("short",st.mean(rs), means_s)
report("all",  st.mean(r for _,r in real), means_a)
