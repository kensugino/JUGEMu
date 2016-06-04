"""

.. module:: findparams
    :synopsis: Using reference annotation and data to find parameters for assembler.

..  moduleauthor:: Ken Sugino <ken.sugino@gmail.com>

"""
# system imports
import subprocess
import multiprocessing
import gzip
import os
import time
from functools import reduce
from operator import iadd, iand
from collections import Counter
from itertools import repeat
import logging
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)
import json

# 3rd party imports
import pandas as PD
import numpy as N
import matplotlib.pyplot as P

# library imports
from jgem import utils as UT
from jgem import bedtools as BT
from jgem import bigwig as BW
from jgem import gtfgffbed as GGB

from jgem.bigwig import BWObj #, BWs 

from sklearn.linear_model import LogisticRegression

def find_maxgap(arr, emin, emax, th, win, gapmode):
    if (emin>th):
        return 0
    if (emax<=th):
        return win
    idx0 = N.nonzero(arr>th) # first find furthest point
    maxpos = idx0[0][-1] # after this all zero
    idx = N.nonzero(arr<=th)
    if len(idx[0])==0:
        return 0
    if (gapmode!='i')&(idx[0][0]>maxpos):
        return 0
    cmax = 1
    cst = idx[0][0]
    ced = cst
    for i in idx[0][1:]:
        if (i>maxpos)&(gapmode!='i'):
            break        
        if i==ced+1: # continuous
            ced = i
        else:
            cmax = max(cmax, ced-cst+1)
            cst = ced = i
    cmax = max(cmax, ced-cst+1)
    return cmax

def find_firstgap(arr, emin, emax, th, win):
    if (emin>th):
        return 0, win # no gap
    if (emax<=th):
        return win, 0 # max
    idx = N.nonzero(arr<=th) # gap pos
    cst = idx[0][0] # current gap start
    ced = cst # current gap end
    for i in idx[0][1:]:
        if i==ced+1: # continuous
            ced = i
        else: # end of first gap and pos
            break
    return ced-cst+1, cst
      
def average(arr, n):
    end =  n * int(len(arr)/n)
    return N.mean(arr[:end].reshape(-1, n), 1)

class BWs(object):

    def __init__(self, paths):
        self.bwobjs = [BWObj(p) for p in paths]

    def __enter__(self):
        for b in self.bwobjs:
            b.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        for b in self.bwobjs:
            b.__exit__(exc_type, exc_value, traceback)

    def get(self, chrom, st, ed):
        a = self.bwobjs[0].get(chrom, st, ed)
        for b in self.bwobjs[1:]:
            a += b.get(chrom, st, ed)
        return a

COPYCOLS = ['chr','st','ed','_gidx','locus','gene_id','gene_type']
CALCFLUXCOLS = ['_id', 'sdelta','ecovavg','ecovmin','ecovmax',
                'sin','sout','ein','eout','sdin','sdout']

class ParamFinder(object):
    """
    Args:
        refpre: pathprefix to ref (assume .ex.txt.gz, .sj.txt.gz)

    """
    def __init__(self, refpre, bwpre, refcode, genome):
        self.refpre = refpre
        self.genome = genome
        self.bwpre = bwpre
        self.refcode = refcode
        self.ex = ex = UT.read_pandas(self.refpre+'.ex.txt.gz')
        self.sj = sj = UT.read_pandas(self.refpre+'.sj.txt.gz')
        self.set_bws(bwpre)

    def process(self, np=10):
        self.extract_all()
        for x in ['ne_i','ne_5','ne_3','e5i','e3i','e53']:
            print('  #{0}:{1}'.format(x, len(getattr(self, x))))
        self.calc_53_params(np=np)
        self.calc_53gap_params(np=np)
        self.calc_exon_params(np=np)
        
    def set_bws(self, bwpre):
        self.bwpre = bwpre
        S2S = {'+':'.p','-':'.n','.':'.u'}
        self.bwpaths = bwp = {
            'ex': {s:bwpre+'.ex{0}.bw'.format(S2S[s]) for s in S2S},
            'sj': {s:bwpre+'.sj{0}.bw'.format(S2S[s]) for s in S2S},
        }
        self.bws = make_bws(bwp)
        
    def extract_all(self):
        self.extract_nonovl_exons()
        self.extract_exi53()
        self.extract_53_pair() # intergenic

    def extract_nonovl_exons(self):
        ex = self.ex
        sj = self.sj
        # nonovl exons    
        ex['gene_type'] = ex['extra'].str.split(';').str[2].str.split().str[1].str[1:-1]
        cols0 = ['chr','st','ed','_id']
        a = self.refpre+'.ex.bed.gz'
        a = UT.write_pandas(ex[cols0], a, '')
        b = self.refpre+'.sj.bed.gz'
        b = UT.write_pandas(sj[cols0], b, '')
        c1 = self.refpre+'.ex-ovl-sj.txt.gz'
        c2 = self.refpre+'.ex-ovl-ex.txt.gz'
        c1 = BT.bedtoolintersect(a,b,c1,wao=True)
        c2 = BT.bedtoolintersect(a,a,c2,wo=True)

        cols = cols0+['b_'+x for x in cols0]+['ovl']
        sov = UT.read_pandas(c1, names=cols)
        sov['len'] = sov['ed']-sov['st']
        sov['ovlratio'] = sov['ovl']/sov['len']
        sovg = sov.groupby('_id')['ovlratio'].max()
        snonov = sovg[sovg<1.] # not completely covered by junction

        eov = UT.read_pandas(c2, names=cols)
        eovsize = eov.groupby('_id').size()
        enonov = eovsize[eovsize==1] # only overlaps with self

        LOG.info('#non-ex-ovl-ex={0}, #non-sj-ovl-ex={1}'.format(len(enonov), len(snonov)))
        ids = set(enonov.index).intersection(snonov.index)
        LOG.info('#non-ovl-ex={0}'.format(len(ids)))
        self.nov_ex = novex = ex.set_index('_id').ix[ids].sort_values(['chr','st','ed']).reset_index()
        novex['len'] = novex['ed']-novex['st']
        self.ne_i = novex[novex['cat']=='i']
        self.ne_5 = novex[novex['cat']=='5']
        self.ne_3 = novex[novex['cat']=='3']
        self.ne_s = novex[novex['cat']=='s']

    def extract_exi53(self):
        # internal exons overlapping with either 5 or 3 prime exons?
        cols0 = ['chr','st','ed','_id', 'sc1','strand']
        cols = cols0+['b_'+x for x in cols0]+['ovl']
        ex = self.ex

        exi = ex[ex['cat']=='i'] # internal exons
        ai = self.refpre + '.exi.bed.gz'
        ai = UT.write_pandas(exi[cols0], ai, '')

        e5 = ex[ex['cat']=='5']
        a5 = self.refpre + '.ex5.bed.gz'
        a5 = UT.write_pandas(e5[cols0], a5, '')

        e3 = ex[ex['cat']=='3']
        a3 = self.refpre + '.ex3.bed.gz'
        a3 = UT.write_pandas(e3[cols0], a3, '')

        a5i = self.refpre + '.ex5-ovl-exi.txt.gz'
        a3i = self.refpre + '.ex3-ovl-exi.txt.gz'

        nc = len(cols0)
        e5i0 = BT.calc_ovlratio(a5,ai,a5i,nc,nc)
        e3i0 = BT.calc_ovlratio(a3,ai,a3i,nc,nc)

        self.e5i = e5i = e5i0[e5i0['ovlratio']==1].rename(columns={'name':'_id'})
        self.e3i = e3i = e3i0[e3i0['ovlratio']==1].rename(columns={'name':'_id'})
    
    def calc_flux_mp(self, beddf, np=10):
        chroms = UT.chroms(self.genome)
        args = []
        for c in chroms:
            bedc = beddf[beddf['chr']==c]
            if len(bedc)>0:
                args.append((bedc, self.bwpaths.copy()))
        rslts = UT.process_mp(calc_flux_chr, args, np=np, doreduce=True)
        df = PD.DataFrame(rslts, columns=CALCFLUXCOLS)
        exdfi = beddf.set_index('_id').ix[df['_id'].values]
        for f in COPYCOLS:
            if f in exdfi:
                df[f] =exdfi[f].values
        df['len'] = df['ed']-df['st']
        return df
    
    def calc_53_params(self, np=10):
        # get parameters
        fname = self.bwpre+'.{0}.flux.txt.gz'.format(self.refcode)
        if os.path.exists(fname):
            D = UT.read_pandas(fname)
        else:
            dic = {}
            for x in ['ne_i','ne_5','ne_3','e5i','e3i']:
                df = getattr(self, x)
                print('calculating {0}...'.format(x))
                dic[x] = self.calc_flux_mp(df, np=np)
                UT.write_pandas(self.bwpre+'.{0}.{1}.flux.txt.gz'.format(self.refcode,x),'h')
            dicb = {}
            for x in ['ne_5','ne_3','e5i','e3i']:
                f = dic[x]
                f['kind'] = 1
                idx = N.abs(N.log2(f['sin']+1)-N.log2(f['sout']+1))>1
                idx = idx & (f['sdin']!=0)|(f['sdout']!=0) # should have either in or out
                dicb[x] = f[idx]
            f = dic['ne_i']
            f['kind'] = 0
            idx = (f['ecovmax']>1)&((f['sdin']!=0)&(f['sdout']!=0)) # should have both in&out
            dicb['ne_i'] = f[idx]
            D = PD.concat(dicb.values(),ignore_index=True)
            UT.write_pandas(D, fname, 'h')

        D['lsin'] = N.log2(D['sin']+1)
        D['lsout'] = N.log2(D['sout']+1)
        D['sdiff'] = N.abs(D['lsin']-D['lsout'])
        D['smean'] = (D['lsin']+D['lsout'])/2.
        X = D[['sdiff','smean']].values
        Y = D['kind'].values
        lr = LogisticRegression()
        lr.fit(X,Y)
        Z = lr.predict(X)
        # save fit coefficients
        ppath = self.bwpre+'.{0}.e53params.json'.format(self.refcode)
        self.write_params(ppath, lr, Y, Z, ['sdiff','smean'])
        # save scatter plots
        spath = self.bwpre+'.{0}.e53params.png'.format(self.refcode)
        title = self.bwpre.split('/')[-1]
        self.plot_sin_sout(dicb, D, Y, Z, spath, title)
        return locals()

    def write_params(self, ppath, lr, Y, Z, cols):
        sen,spe = calc_sensitivity_specificity(Y,Z)
        b1 = list(lr.coef_[0])
        b0 = lr.intercept_[0]
        print('b1={0}, b0={1}'.format(b1,b0))
        params = dict(cols=cols,coef=b1,intercept=b0,sensitivity=sen, specificity=spe)
        with open(ppath,'w') as fp:
            json.dump(params, fp)


    def plot_sin_sout(self, dicb, D, Y, Z, spath=None, title='', alpha=0.1):
        fig,axr = P.subplots(2,2,figsize=(8,8),sharex=True,sharey=True)
        P.subplots_adjust(hspace=0.1,wspace=0.1)
        def _plt(Dsub, c, ax):
            # Dsub = D[K==k]
            x = N.log2(Dsub['sin']+1)
            y = N.log2(Dsub['sout']+1)
            ax.plot(x,y,c,alpha=alpha, ms=4)
        # 0,0 ne_i vs ne_5,ne_3
        _plt(dicb['ne_i'], 'b.', axr[0][0])
        _plt(dicb['ne_5'], 'r.', axr[0][0])
        _plt(dicb['ne_3'], 'r.', axr[0][0])
        axr[0][0].set_title('edge 53 exons')
        # 0,1 ne_i vs e5i,e3i
        _plt(dicb['ne_i'], 'b.', axr[0][1])
        _plt(dicb['e5i'], 'r.', axr[0][1])
        _plt(dicb['e3i'], 'r.', axr[0][1])
        axr[0][1].set_title('internal 53 exons')
        # 1,0 Y
        _plt(D[Y==0], 'b.', axr[1][0])
        _plt(D[Y==1], 'r.', axr[1][0])
        axr[1][0].set_title('non zero subsets')
        # 1,1 Z
        _plt(D[Z==0], 'b.', axr[1][1])
        _plt(D[Z==1], 'r.', axr[1][1])
        axr[1][1].set_title('logistic regression')

        axr[0][0].set_ylabel('log2(junction outflux)')
        axr[1][0].set_ylabel('log2(junction outflux)')
        axr[1][0].set_xlabel('log2(junction influx)')
        axr[1][1].set_xlabel('log2(junction influx)')
        vmax = N.floor(N.log2(max(D['sin'].max(),D['sout'].max())+1))-1
        axr[0][0].set_xlim(-1,vmax)
        axr[0][0].set_ylim(-1,vmax)

        fig.suptitle(title)
        if spath is not None:
            fig.savefig(spath)


    def calc_53gap_params(self, np=10):
        d5path = self.bwpre+'.{0}.gap5params.txt.gz'.format(self.refcode)
        d3path = self.bwpre+'.{0}.gap3params.txt.gz'.format(self.refcode)
        if os.path.exists(d5path):
            d5 = UT.read_pandas(d5path)
        else:
            d5 = self.calc_params_mp(self.ne_5, win=8192, np=np, gapmode='53', direction='<')
            UT.write_pandas(d5, d5path, 'h')
        if os.path.exists(d3path):
            d3 = UT.read_pandas(d3path)
        else:
            d3 = self.calc_params_mp(self.ne_3, win=8192, np=np, gapmode='53', direction='>')
            UT.write_pandas(d3, d3path, 'h')

        i5 = (d5['sOut']>0)&(d5['emax']>0)
        i3 = (d3['sIn']>0)&(d3['emax']>0)
        d50 = d5[i5]
        d30 = d3[i3]
        def _fitone(d0, x, y1, y2):
            da = d0[[x,y1]].copy().rename(columns={y1:'gap',x:'sin'})
            db = d0[[x,y2]].copy().rename(columns={y2:'gap',x:'sin'})
            da['kind'] = 1
            db['kind'] = 0
            D = PD.concat([da,db],ignore_index=True)
            D['lsin'] = N.log2(D['sin']+1)
            D['lgap'] = N.log2(D['gap']+1)
            X = D[['lsin','lgap']].values
            Y = D['kind'].values
            lr = LogisticRegression()
            lr.fit(X,Y)
            Z = lr.predict(X)
            return locals()
        # fit5_005 = _fitone(d50,'sOut','gap005','gapIn')
        # fit5_002 = _fitone(d50,'sOut','gap002','gapIn')
        fit5_000 = _fitone(d50,'sOut','gap000','gapIn')
        # fit3_005 = _fitone(d30,'sIn', 'gap005','gapOut')
        # fit3_002 = _fitone(d30,'sIn', 'gap002','gapOut')
        fit3_000 = _fitone(d30,'sIn', 'gap000','gapOut')
        # save coefs
        p5path = self.bwpre+'.{0}.gap5params.json'.format(self.refcode)
        f = fit5_000
        self.write_params(p5path, f['lr'], f['Y'], f['Z'], ['lsin','lgap'])
        p3path = self.bwpre+'.{0}.gap3params.json'.format(self.refcode)
        f = fit3_000
        self.write_params(p3path, f['lr'], f['Y'], f['Z'], ['lsin','lgap'])

        # save scatter plots
        spath = self.bwpre+'.{0}.gap53params.png'.format(self.refcode)
        title = self.bwpre.split('/')[-1]
        self.plot_gap53_fit(fit5_000, fit3_000, spath, title)

        return locals()

    def plot_gap53_fit(self, lcls5, lcls3, spath, title):
        fig,axr = P.subplots(2,2,figsize=(8,8), sharex=True, sharey=True)
        P.subplots_adjust(hspace=0.1,wspace=0.1)

        def _one(W,X,ax,title):
            X0 = X[W==0]
            X1 = X[W==1]
            x0 = X0[:,0]
            y0 = X0[:,1]
            x1 = X1[:,0]
            y1 = X1[:,1]
            ax.plot(x0,y0,'r.', ms=5, alpha=0.1)
            ax.plot(x1,y1,'b.', ms=5, alpha=0.1)
            ax.set_title(title)

        _one(lcls5['Z'],lcls5['X'], axr[0][0],'5 predict')
        _one(lcls5['Y'],lcls5['X'], axr[0][1],'5 actual')
        _one(lcls3['Z'],lcls3['X'], axr[1][0],'3 predict')
        _one(lcls3['Y'],lcls3['X'], axr[1][1],'3 actual')
        axr[1][0].set_xlabel('log2(junction influx)')
        axr[0][0].set_ylabel('log2(gap size)')
        axr[1][1].set_xlabel('log2(junction influx)')
        axr[1][0].set_ylabel('log2(gap size)')
        axr[0][0].set_xlim(-1, 6)
        axr[0][0].set_ylim(-1,14)
        fig.suptitle(title)

        fig.savefig(spath)
        
    def calc_exon_params(self, np=10):
        # get params
        neipath = self.bwpre+'.{0}.nei.params.txt.gz'.format(self.refcode)
        e53path = self.bwpre+'.{0}.e53.params.txt.gz'.format(self.refcode)
        if os.path.exists(neipath):
            nei = UT.read_pandas(neipath)
        else:
            nei = self.calc_params_mp(self.ne_i, np=np, gapmode='i') # ~ 1min
            UT.write_pandas(nei, neipath, 'h')
        if os.path.exists(e53path):
            e53 = UT.read_pandas(e53path)
        else:
            e53 = self.calc_params_mp(self.e53, np=np, gapmode='i') # ~ 10min don't do long ones stupid
            UT.write_pandas(e53, e53path, 'h')
        # logistic fit
        cols =  ['chr', 'st', 'ed', 'gap005', 'emax', 'emin', 'sIn', 'sOut', 'locus', 'kind','len', 'sdIn','sdOut']
        nei['kind'] = 1
        e53['kind'] = 0
        nei['len'] = nei['ed'] - nei['st']
        e53['len'] = e53['ed'] - e53['st']
        D = PD.concat([nei[cols], e53[cols]],ignore_index=True)
        D['llen'] = N.log10((D['len']))
        D['lgap'] = N.log10(D['gap005']+1)
        D['lemax'] = N.log2(D['emax']+1)
        D1 = D[(D['emax']>0)&(D['sdIn']!=0)&(D['sdOut']!=0)]
        print(len(D), len(D1))
        X = D1[['lemax', 'lgap','llen']].values
        Y = D1['kind'].values
        lr = LogisticRegression()
        lr.fit(X,Y)
        Z = lr.predict(X)    
        # write json
        ppath = self.bwpre+'.{0}.exonparams.json'.format(self.refcode)
        self.write_params(ppath, lr, Y, Z, ['lemax','lgap','llen'])
        # make fig
        spath = self.bwpre+'.{0}.exonparams.png'.format(self.refcode)
        title = self.bwpre.split('/')[-1]
        self.plot_exon_fit(spath, title, X, Y, Z)

        return locals()

    def plot_exon_fit(self, spath, title, X, Y, Z):
        fig,axr = P.subplots(2,2,figsize=(8,8), sharex=True, sharey=True)
        P.subplots_adjust(hspace=0.1,wspace=0.2)

        def _row(W,t0,t1,ax):
            idx0 = W==0
            idx1 = W==1
            px1 = X[idx1,0] # lemax
            px0 = X[idx0,0]
            pz1 = X[idx1,1] # lgap
            pz0 = X[idx0,1]
            py1 = X[idx1,2] # llen
            py0 = X[idx0,2]
            ax[0].plot(px1,pz1,'r.',ms=3,alpha=0.3)
            ax[0].plot(px0,pz0,'b.',ms=3,alpha=0.3)
            ax[1].plot(px1,py1,'r.',ms=3,alpha=0.3)
            ax[1].plot(px0,py0,'b.',ms=3,alpha=0.3)
            ax[0].set_title(t0)
            ax[1].set_title(t1)

        _row(Y, 'actual log(gap)', 'actual log(len)', axr[0])
        _row(Z, 'fit log(gap)', 'fit log(len)', axr[1])
        axr[1][0].set_xlabel('log2(ecov max)')
        axr[1][1].set_xlabel('log2(ecov max)')
        axr[0][0].set_ylabel('log10(len)')
        axr[1][0].set_ylabel('log10(len)')
        axr[0][0].set_xlim(-1, 6)
        axr[0][0].set_ylim(-1, 5)
        axr[1][0].set_ylim(-1, 5)
        fig.suptitle(title)
        fig.savefig(spath)
        

    def extract_53_pair(self):
        # between genes
        ex = self.ex
        tmpprefix = self.refpre
        ex['_apos'] = ex['a_pos'].str.split(':').str[1].astype(int)
        ex['_dpos'] = ex['d_pos'].str.split(':').str[1].astype(int)
        ex.loc[ex['cat']=='3','spos'] = ex['_apos']
        ex.loc[ex['cat']=='5','spos'] = ex['_dpos']
        cols = ['chr','st','ed','name','strand','_gidx1','_gidx2']
        def _find(ecs, chrom, strand):
            e53 = ecs[ecs['cat'].isin(['3','5'])].sort_values('spos')
            #esorted = echrstrand.sort_values('_apos')
            v1 = e53.iloc[:-1][['spos','cat','_gidx','_id','st','ed']].values
            v2 = e53.iloc[1:][['spos','cat','_gidx','_id','st','ed']].values
            pairs = []
            if strand=='+':
                for r1,r2 in zip(v1,v2):
                    if r1[2]!=r2[2]: # not same gene
                        if (r1[1]=='3')&(r2[1]=='5')&(r1[5]<r2[4]): # non overlapping 3=>5
                            name = '+g{0}e{1}|g{2}e{3}'.format(r1[2],r1[3],r2[2],r2[3])
                            pairs.append((chrom,r1[0],r2[0],name,strand,r1[2],r2[2]))
            else:
                for r1,r2 in zip(v1,v2):
                    if r1[2]!=r2[2]:
                        if (r1[1]=='5')&(r2[1]=='3')&(r1[5]<r2[4]): # 
                            name = '-g{0}e{1}|g{2}e{3}'.format(r1[2],r1[3],r2[2],r2[3])
                            pairs.append((chrom,r1[0],r2[0],name,strand,r1[2],r2[2]))

            df = PD.DataFrame(pairs, columns=cols)
            return df
        rslts = []
        for chrom in ex['chr'].unique():
            for strand in ['+','-']:
                echrstrand = ex[(ex['chr']==chrom)&(ex['strand']==strand)]
                rslts.append(_find(echrstrand, chrom, strand))
        df = PD.concat(rslts, ignore_index=True).sort_values(['chr','st','ed'])
        # intersect with internal exons
        a = tmpprefix+'.53.exi.bed' # ncol 3
        b = tmpprefix+'.53.bed' #ncol 5
        c = tmpprefix+'.53.exi.ovl.txt'
        exi = ex[ex['cat']=='i'].sort_values(['chr','st','ed'])
        UT.write_pandas(exi[['chr','st','ed']], a, '')
        UT.write_pandas(df, b, '')
        c = BT.bedtoolintersect(b, a, c, wao=True)
        cols1 = cols+['b_chr','b_st','b_ed','ovl']
        cdf = UT.read_pandas(c, names=cols1)
        sdf = cdf[cdf['ovl']==0][cols]
        sdf['locus'] = UT.calc_locus(sdf)
        sdf['len'] = sdf['ed']-sdf['st']
        maxexonsize = self.ne_i['len'].max()
        sdf = sdf[(sdf['len']>20)&(sdf['len']<max(2*maxexonsize, 20000))]
        UT.write_pandas(sdf, tmpprefix+'.e53pair.bed.gz')
        sdf.index.name='_id'
        
        self.e53 = sdf.reset_index()

    def calc_params_mp(self, beddf,  win=600, siz=10, direction='>', gapmode='53', np=10):
        chroms = UT.chroms(self.genome)
        args = []
        for c in chroms:
            bedc = beddf[beddf['chr']==c]
            if len(bedc)>0:
                args.append((bedc, self.bwpaths.copy(), win, siz, direction, gapmode))
        rslts = UT.process_mp(calc_params_chr, args, np=np, doreduce=True)
        df = PD.DataFrame(rslts, columns=CALCPARAMCOLS)
        exdfi = beddf.set_index('_id').ix[df['_id'].values]
        for f in COPYCOLS:
            if f in exdfi:
                df[f] =exdfi[f].values
        df['len'] = df['ed']-df['st']
        return df
        
    def parseplot(self, locus, figsize=(15,6)):
        bws = self.bws
        chrom,tmp,strand = locus.split(':')
        st,ed = [int(x.replace(',','')) for x in tmp.split('-')]
        print(locus,st,ed,strand)
        a1 = bws['ex'][strand].get_as_array(chrom,st,ed)
        b1 = bws['sj'][strand].get_as_array(chrom,st,ed)
        fig,axr = P.subplots(2,1,figsize=figsize)
        axr[0].plot(a1)
        axr[0].plot(b1)
        axr[1].plot(a1)
        axr[1].plot(a1+b1,'r--')
    
    def plotex(self, exrec, win=50, figsize=(15,6)):
        chrom,st,ed,strand = exrec[['chr','st','ed','strand']].values
        print(chrom,st,ed,strand)
        bws = self.bws
        a1 = bws['ex'][strand].get_as_array(chrom,st-win,ed+win)
        b1 = bws['sj'][strand].get_as_array(chrom,st-win,ed+win)
        fig,ax = P.subplots(1,1,figsize=figsize)
        ax.plot(a1)
        ax.plot(b1)
        # ex: max,min,maxl,maxr,exl10,exr10 sj: sjl10, sjr10
        dic = dict(
            exmax = N.max(a1[win:-win]),
            exmin = N.min(a1[win:-win]),
            maxl = N.max(a1[:win]),
            maxr = N.max(a1[-win:]),
            exl10 = N.mean(a1[win:win+10]),
            exr10 = N.mean(a1[-win-10:-win]),
            sjl10 = N.mean(b1[win-10:win]),
            sjr10 = N.mean(b1[-win:-win+10]),
        )
        return dic
    
    def pltscatter(self, eids, exdf, sidf, fld='d_id', ecov='ecov', jcov='jcov'):
        etgt = exdf.set_index('_id').ix[eids]
        #e1d = etgt[etgt[fld]!=0].groupby(fld)['ecov_d_m35'].mean()
        e1d = etgt[etgt[fld]!=0].set_index(fld)[ecov]
        s1d = g4s1.groupby(fld)[jcov].sum()
        print(len(e1d), len(s1d))
        # all exons
        x = N.log2(e1d.values+1)
        y = N.log2(s1d.ix[e1d.index].values+1)
        P.plot(x, y, '.', ms=1);
        x0 = N.linspace(0,20)
        P.plot(x0,x0,'r')
        P.plot(x0,x0+1.5,'r--')
        P.plot(x0,x0-1.5,'r--')
        P.xlabel('ecov mean')
        P.ylabel('jcov sum')
        return x,y

    def analyze_i(self, t, bnum=1000):
        xfld = 'emax'
        yfld = 'gap'
        ymax = 14
        #t = pi
        #bnum = 1000
        y2fld = 'mp'
        y2max = 1.1

        t['len'] = t['ed']-t['st']
        t['mp'] = 1. - t[['gap','len']].min(axis=1)/t['len']
        t['mima'] = t['emin']/t['emax']
        t = t[t['emax']>0]
        
        fig,axr = P.subplots(1,3,figsize=(12,4))
        ax = axr[0]
        x = N.log2(t[xfld].values+1)
        y = N.log2(t['gap'].values+1)
        ax.plot(x,y,'.',ms=5,alpha=0.1)
        ax.plot([0,ymax],[0,ymax],'g--')
        ax.set_ylim([-1,ymax])
        avx,avy = UT.calc_binned(x,y,num=bnum)
        ax.plot(avx,avy,'r.-')
        ax.set_title('gap')

        ax = axr[1]
        x = N.log2(t[xfld].values+1)
        y = N.log2(t['mima'].values+1)
        ax.plot(x,y,'.',ms=5,alpha=0.1)
        ax.plot([0,y2max],[0,y2max],'g--')
        ax.set_ylim([-0.1,y2max])
        avx,avy = UT.calc_binned(x,y,num=bnum)
        ax.plot(avx,avy,'r.-')
        ax.set_title('mima')

        ax = axr[2]
        x = N.log2(t['sIn'].values+1)
        y = N.log2(t['emin'].values+1)
        ax.plot(x,y,'.',ms=5,alpha=0.1)
        ax.plot([0,ymax],[0,ymax],'g--')
        # ax.set_ylim([-0.1,1.1])
        avx,avy = UT.calc_binned(x,y,num=bnum)
        ax.plot(avx,avy,'r.-')
        ax.set_title('emin')
    
    
def calc_sensitivity_specificity(Y,Z):
    print('mismatch:{0}/{1}'.format(N.sum(Y!=Z), len(Y)))
    num53 = N.sum(Y==1)
    numi = N.sum(Y==0)
    num53tp = N.sum((Y==1)&(Z==1))
    num53fn = N.sum((Y==1)&(Z==0))
    num53fp = N.sum((Y==0)&(Z==1))
    num53tn = N.sum((Y==0)&(Z==0))
    print('TP({0}),FN({1}),TN({2}),FP({3})'.format(num53tp, num53fn, num53tn, num53fp))
    sensitivity = float(num53tp)/(num53tp+num53fn)
    specificity = 1.-float(num53fp)/(num53fp+num53tn)
    print('sensitivity={0:.3f}, specificity={1:.3f}'.format(sensitivity, specificity))
    return sensitivity, specificity



    
def make_bws(bwp):
    # .ex.p.bw, .ex.n.bw, .ex.u.bw, .sj.p.bw, .sj.n.bw, .sj.u.bw
    bws = {'ex':{},'sj':{}}
    bws['ex']['.'] = BWs([bwp['ex']['.']])
    bws['ex']['+'] = BWs([bwp['ex']['+'],bwp['ex']['.']]) if os.path.exists(bwp['ex']['+']) else bws['ex']['.']
    bws['ex']['-'] = BWs([bwp['ex']['-'],bwp['ex']['.']]) if os.path.exists(bwp['ex']['-']) else bws['ex']['.']
    bws['sj']['+'] = BWs([bwp['sj']['+'],bwp['sj']['.']])
    bws['sj']['-'] = BWs([bwp['sj']['-'],bwp['sj']['.']])
    bws['sj']['.'] = BWs([bwp['sj']['.']])
    return bws

CALCPARAMCOLS = ['_id','emax','emin',
            'emaxIn','eminIn','gapIn','gposIn',
            'emaxOut','eminOut','gapOut','gposOut',
            'eIn','sIn','sdIn',
            'eOut','sOut','sdOut',
            'gap000', 'gap001', 'gap002','gap005']#,'gap010','gap015','gap020']

def calc_params_chr(exdf, bwp, win=300, siz=10,  direction='>', gapmode='i'):
    bws = make_bws(bwp)
    ebw = bws['ex']
    sbw = bws['sj']
    recs = []
    cols =CALCPARAMCOLS
    for strand in ['+','-','.']:
        exdfsub = exdf[exdf['strand']==strand]
        with ebw[strand]:
            with sbw[strand]:
                for chrom,st,ed,_id in exdfsub[['chr','st','ed','_id']].values:
                    #win = ed-st # same size as exon
                    left = max(0, st-win)
                    if left==0:
                        print('st-win<0:{0}:{1}-{2}'.format(chrom,st,ed))
                    right = ed+win
                    stpos = st-left
                    edpos = ed-left
                    a1 = ebw[strand].get(chrom,left,right)
                    b1 = sbw[strand].get(chrom,left,right)
                     
                    exl10 = N.mean(a1[stpos:stpos+siz])
                    sjl10 = N.mean(b1[stpos-siz:stpos])
                    exr10 = N.mean(a1[edpos-siz:edpos])
                    sjr10 = N.mean(b1[edpos:edpos+siz])
                    sdifl = b1[stpos]-b1[stpos-1]
                    sdifr = b1[edpos]-b1[edpos-1]
                    exmax = N.max(a1[stpos:edpos])
                    exmin = N.min(a1[stpos:edpos])
                    #gapth = sjl10*covfactor if strand=='+' else sjr10*covfactor
                    gaps = {}
                    cfs = [0,0.01,0.02,0.05]#,0.1,0.15,0.2]:
                    for covfactor in cfs:
                        gapth = exmax*covfactor
                        if ((direction=='>')&(strand=='+'))|((direction!='>')&(strand=='-')):
                            gaps[covfactor] = find_maxgap(a1[stpos:edpos],exmin, exmax, gapth, win, gapmode)
                        else:
                            gaps[covfactor] = find_maxgap(a1[stpos:edpos][::-1],exmin, exmax, gapth, win, gapmode)
                    maxl = N.max(a1[:stpos])
                    maxr = N.max(a1[edpos:])
                    minl = N.min(a1[:stpos])
                    minr = N.min(a1[edpos:])
                    gapl,posl = find_firstgap(a1[:stpos][::-1],minl,maxl,gapth,win)
                    gapr,posr = find_firstgap(a1[edpos:],minr,maxr,gapth,win)
                    if strand=='+':
                        recs.append([_id,exmax,exmin,
                                     maxl,minl,gapl,posl, 
                                     maxr,minr,gapr,posr, 
                                     exl10,sjl10,sdifl,
                                     exr10,sjr10,sdifr]+[gaps[x] for x in cfs])
                    else:
                        recs.append([_id,exmax,exmin,
                                     maxr,minr,gapr,posr, 
                                     maxl,minl,gapl,posl, 
                                     exr10,sjr10,sdifr,
                                     exl10,sjl10,sdifl]+[gaps[x] for x in cfs])
    return recs

def calc_flux_chr(exdf, bwp):
    bws = make_bws(bwp)
    ebw = bws['ex']
    sbw = bws['sj']
    recs = []
    cols = CALCFLUXCOLS
    for strand in ['+','-','.']:
        exdfsub = exdf[exdf['strand']==strand]
        with ebw[strand]:
            with sbw[strand]:
                for chrom, st, ed, _id in exdf[['chr','st','ed', '_id']].values:
                    ecov = ebw[strand].get(chrom,st-1,ed+1)
                    scov = sbw[strand].get(chrom,st-1,ed+1)
                    if strand=='+':
                        sd = scov[-1]-scov[0]
                        sin,sout = scov[0],scov[-1]
                        ein,eout = ecov[0],ecov[-1]
                        sdin= scov[1]-scov[0]
                        sdout = scov[-1]-scov[-2]
                    else:
                        sd = scov[0]-scov[-1]
                        sin,sout = scov[-1],scov[0]
                        ein,eout = ecov[-1],ecov[0]
                        sdout= -scov[1]+scov[0]
                        sdin = -scov[-1]+scov[-2]
                    recs.append([_id, sd, ecov.mean(), ecov.min(), ecov.max(),
                                 sin,sout,ein,eout,sdin,sdout])
    return recs
