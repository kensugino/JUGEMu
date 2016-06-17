"""

.. module:: assembler3
    :synopsis: assemble genes from RNASeq data (reads and junction coverages (bigwig) and junction paths)
    jGEM version 3 assembler

..  moduleauthor:: Ken Sugino <ken.sugino@gmail.com>

"""
# system imports
import subprocess
import multiprocessing
import gzip
import os
import time
import shutil
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


# LocalAssembler imports
from collections import Counter
from matplotlib.collections import BrokenBarHCollection
from functools import partial, reduce
from operator import iadd
import bisect
from scipy.optimize import nnls


# library imports
from jgem import utils as UT
from jgem import bigwig as BW
from jgem import bedtools as BT
from jgem import gtfgffbed as GGB
from jgem import taskqueue as TQ
from jgem import graph as GP

# assembler 3
# 1. find exons
# also find exon from multijunction reads
# make exdf sjdf
# 2a. find 53 positions, separate edge case and internal case
# 2b. find 53 exons
# 3. find_all_paths => find connected components, tst group, tst-ted group, 5e-tst-ted-3e group
# 4. cov estimation up to 5e-tst-ted-3e group
# 5. simultaneous cov estimation within 5tt3 group and path generation



# sjpath  => sjdf, exdfi0
#   => edge combinations => classifier => exdfi1

# filled sja => 53 pos_e, 53 pos_i
# edgefinder extension

# span => graph => connected components => tst group, tstted group, 5tt3 group
# cov estimation with tstted group and 5tt3 group

# path generation from highest cov (use cov range search trick)


####### BigWigs ########################################################################
     

class SjExBigWigs(object):
    
    def __init__(self, bwpre, sjbwpre=None, mixunstranded=True):
        if sjbwpre is None:
            sjbwpre = bwpre
        if type(bwpre)!=type([]):
            bwpre = [bwpre]
        if type(sjbwpre)!=type([]):
            sjbwpre = [sjbwpre]
        self.bwpre = bwpre
        self.sjbwpre = sjbwpre
        self.mixunstranded = mixunstranded
        S2S = {'+':'.p','-':'.n','.':'.u','r+':'.rp','r-':'.rn','r.':'.ru'}
        bwp = {
            'ex': {s:[b+'.ex{0}.bw'.format(S2S[s]) for b in bwpre] for s in S2S},
            'sj': {s:[b+'.sj{0}.bw'.format(S2S[s]) for b in sjbwpre] for s in S2S},
        }
        self.bwpaths = bwpaths = {'ex':{},'sj':{}}
        if mixunstranded:
            bwpaths['ex']['+'] = {'p':bwp['ex']['+']+bwp['ex']['.'],}
            bwpaths['ex']['-'] = {'p':bwp['ex']['-']+bwp['ex']['.'],}
            bwpaths['ex']['.'] = {'p':bwp['ex']['.'],}        
            bwpaths['ex']['a'] = {'p':bwp['ex']['+']+bwp['ex']['-']+bwp['ex']['.'],}
            bwpaths['sj']['+'] = {'p':bwp['sj']['+']+bwp['sj']['.'],}
            bwpaths['sj']['-'] = {'p':bwp['sj']['-']+bwp['sj']['.'],}
            bwpaths['sj']['.'] = {'p':bwp['sj']['.'],}
            bwpaths['sj']['a'] = {'p':bwp['sj']['+']+bwp['sj']['-']+bwp['sj']['.'],}
        else:
            bwpaths['ex']['+'] = {'p':bwp['ex']['+'],}
            bwpaths['ex']['-'] = {'p':bwp['ex']['-'],}
            bwpaths['ex']['.'] = {'p':bwp['ex']['.'],}        
            bwpaths['ex']['a'] = {'p':bwp['ex']['+']+bwp['ex']['-'],}
            bwpaths['sj']['+'] = {'p':bwp['sj']['+'],}
            bwpaths['sj']['-'] = {'p':bwp['sj']['-'],}
            bwpaths['sj']['.'] = {'p':bwp['sj']['.'],}
            bwpaths['sj']['a'] = {'p':bwp['sj']['+']+bwp['sj']['-'],}
        
        self.make_bws()
    
    def make_bws(self):
        bwp = self.bwpaths
        self.bws = bws = {}
        for k in ['ex','sj']: 
            # bws[k] = {s: BW.MultiBigWigs(plus=bwp[k][s]['p'],
            #                          minus=bwp[k][s]['n']) for s in ['+','-','.']}
            bws[k] = {s: BW.MultiBigWigs(plus=bwp[k][s]['p']) for s in ['+','-','.','a']}
            for s in bws[k]:
                bws[k][s].make_bws()
        
    def __enter__(self):
        for k in ['ex','sj']:
            for s in self.bws[k]:
                self.bws[k][s].__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        for k in ['ex','sj']:
            for s in self.bws[k]:
                self.bws[k][s].__exit__(exc_type, exc_value, traceback)


####### Classifiers ####################################################################

class LogisticClassifier(object):
    
    def __init__(self, json, dstcol):
        self.json = json
        self.b0 = json['intercept'] #b0
        self.bs = N.array(json['coef']) #bs
        self.cols = json['cols']
        self.dstcol = dstcol
        
    def classify(self, df):
        X = df[self.cols].values
        d = self.b0 + N.dot(X, self.bs)
        e = N.exp(-d)
        p = 1./(1.+e)
        df[self.dstcol] = p>0.5
        

# for intergenic        
itg_p = dict(coef = N.array([ -0.40,  -4.72, 0.86]),
           intercept = 11.8638183,
           cols = ['lemax','lgap','llen'],
           th = 0.05,
           zoom = 1)
INTG = LogisticClassifier(json=itg_p, dstcol='exon')

e53_p = dict(coef = N.array([2.51, -0.77]),
             intercept= -2.7,
             cols = ['sdiff','smean'],
             sdiffth= 1,
             zoom = 1)
E53C = LogisticClassifier(json=e53_p, dstcol='e53')

e53m_p = dict(coef = N.array([1.542, -0.368]),
              intercept = -0.329,
              cols = ['sdiff','smean'],
             sdiffth= 0.5,
             zoom = 1)
E53CM = LogisticClassifier(json=e53m_p, dstcol='e53')


class GapEdgeFinder(object):
    
    def __init__(self, json):
        self.json=json
        a_lsin,a_lgap = json['coef']
        b0 = json['intercept']
        th = json['th']
        self.zoom = zoom = json['zoom']
        self.a_lsin = a_lsin
        self.a_lgap = a_lgap
        self.b0 = b0
        self.c0 = -b0/a_lgap
        self.c1 = -a_lsin/a_lgap
        self.th = th
        self.maxsize = json['maxsize']
        
    def find(self, sja, exa, direction):
        # sja1, exa1 : pos0=>pos+1(<), pos-1=>pos0(>)
        c0,c1,th = self.c0,self.c1,self.th
        # def _find_gap_from_idx(idx):
        #     if len(idx)==0:
        #         return
        #     i0 = idx[0] # current gap start
        #     i1 = i0 # current gap end
        #     for i2 in idx[1:]: # next point <= th
        #         if (i1+1)!=i2: # not continuous
        #             yield (i0, i1-i0+1) # position and gapsize
        #             i0 = i2 # new start
        #         i1 = i2
        #     if i0!=i1:
        #         yield (i0, i1-i0+1)
        zoom = self.zoom
        def _find_gap_from_idx(idx):
            if len(idx)==0:
                return []
            dif = idx[1:]-idx[:-1] # if continuous dif==1
            idx2 = N.nonzero(dif>1)[0] # non contiguous   
            idxst = N.array([idx[0]]+list(idx[idx2+1]))
            idxed = N.array(list(idx[idx2])+[idx[-1]])
            gsize = idxed - idxst + 1
            return zip(idxst, gsize)
        if direction=='>':
            # lsin = abs(sja[1]-sja[0])
            ein = N.mean(exa[1:11])
            lein = N.log2(zoom*ein+1)
            gapth = 2**(c0+c1*lein)-1
            #print('gapth={0:.2f}, lsin={1:.2f}'.format(gapth, lsin))
            # pos => pos0, find position where lgap > gapth
            idx = N.nonzero(exa[1:]<=th*ein)[0]
            #print(idx)
            epos = len(exa)-1 # all the way to the end
            for x in _find_gap_from_idx(idx):
                if x[1]>gapth: # found
                    epos = x[0] # start offset pos
                    break
            epos = min(epos, self.maxsize)
        else:
            # lsin = abs(sja[-1]-sja[-2])
            ein = N.mean(exa[-12:-1])
            lein = N.log2(zoom*ein+1)
            gapth = 2**(c0+c1*lein)-1
            #print('gapth={0:.2f}, lsin={1:.2f}'.format(gapth, lsin))
            # pos0 <= pos, going opposite way
            idx = N.nonzero(exa[:-1][::-1]<=th*ein)[0]
            epos = -len(exa)
            for x in _find_gap_from_idx(idx):
                if x[1]>gapth:
                    epos = -x[0]+1
                    break
            epos = max(epos, -self.maxsize)            
        return epos
        
EF5JSON = dict(coef=[-0.285,-0.81], intercept=5.6, th=0, zoom=1, maxsize=3000)
# EF5 = EdgeFinder(EF5JSON)
EF3JSON = dict(coef=[-0.25,-0.51], intercept=4.5, th=0, zoom=1, maxsize=20000) # -0.25, -0.5, 4.5
# EF3 = EdgeFinder(EF3JSON) 

class EdgeFinder(object):

    def __init__(self, json, use_ef2=True):
        self.gap_ef = GapEdgeFinder(json)
        self.use_ef2 = use_ef2
        if use_ef2:
            self.slope_ef = SlopeEdgeFinder(json)

    def find(self, sja, exa, direction):
        epos = self.gap_ef.find(sja, exa, direction)
        if self.use_ef2 and  N.abs(epos)>10:
            if direction=='<':
                sja1 = sja[epos:]
                exa1 = exa[epos:]
            else:
                sja1 = sja[:epos]
                exa1 = exa[:epos]
            epos2 = self.slope_ef.find(sja1,exa1,direction)
            if len(epos2)>0:
                return epos2
            return [epos]
        else:
            return [epos]


SEDJSON = dict(
    smwinsize=151,# smwinsize (int): (default 151)
    minintsize=10,# minintsize (int): (default 10)
    aggregateratio=0.1,# aggregateratio (float): (default 0.1)
    winsize=15,# winsize (int): (default 15)
    minth=0.5,# minth (float):  (default 0.5)
    sigmath=3,# sigmath (float): sigma threshold (default 3)
    mimath=0.15,# mimath (float): min, max threshold (default 0.15)
    mimath2=None,# mimath2 (float): (default None)
    triggerth=2,# triggerth (float): (default 2)
    covth=0.005,# covth (float): (default 0.005)
    covratio=0.1,# covratio (float): (default 0.1)
)

class SlopeEdgeFinder(object):

    def __init__(self, json, verbose=False):
        self.json = json
        self.smwinsize=json.get('smwinsize', 151)
        self.swin = N.ones(self.smwinsize)
        self.minintsize = json.get('minintsize', 10)
        self.aggregateratio = json.get('aggregateratio', 0.1)
        self.winsize = json.get('winsize', 15)
        self.win = N.ones(self.winsize)
        self.minth = json.get('minth', 0.5)
        self.sigmath = json.get('sigmath', 3)
        self.mimath = json.get('mimath', 0.15)
        self.mimath2 = json.get('mimath2', None)
        if self.mimath2 is None:
            self.mimath2 = min(0.5, self.mimath*2.5)
        self.triggerth = json.get('triggerth', 2)
        self.covth = json.get('covth', 0.005)
        self.covratio = json.get('covratio', 0.1)
        self.verbose = verbose

    def find(self, sja, exa, direction):
        # sja, exa : pos0=>pos+1(<), pos-1=>pos0(>)        
        sws = self.smwinsize # smooth window for abs cov th detection
        swin = self.swin
        if direction=='<':
            v = exa[:-1]
        else:
            v = exa[1:]
        v0 = N.concatenate([swin*v[0], v, swin*v[-1]])
        sm = N.convolve(v0, swin/float(sws), 'same')[sws:-sws]
        return self.fix(v, sm, direction)

    def fix(self, v, sm, direction):
        if direction=='<':
            v = v[::-1]
            sm = sm[::-1]
        olen = len(v)
        l0,l1,l2,l3,l4 = 0,0,0,0,0
        eds0 = self.detect_sharp_drops(v, olen)
        l0 = len(eds0)
        eds1 = self.detect_rise(v)
        l1 = len(eds1)
        if l1==0:
            eds1 = self.detect_low_level(v)
            l2 = len(eds1)
            if l2==0 and l0==0:
                eds1 = self.detect_min(v)
                l3 = len(eds1)
                if l3==0:# nothing detected, last resort
                    eds1 = self.detect_rise2(v)
                    l4 = len(eds1)
        if self.verbose:
            LOG.debug('drop({0}), rise({1}), low({2}), min({3}), rise2({4})'.format(l0,l1,l2,l3,l4))
        if len(eds1)>0:
            eds1 = self.trim(v,sm,eds1)
        eds = eds0+eds1  
        if len(eds)==0:
            eds = [olen]
        eds = self._aggregate(olen,eds)
        if len(eds)==0:
            return []
        if direction=='<':
            return [-x for x in eds]
        return eds

    def trim(self, v, sm, eds):
        # just trim left most
        th1 = self.covth # absolute min
        #th2 = 2**(N.max(N.log2(v+1))*self.covratio)-1 # relative conservative
        eidx=eds[-1]
        if eidx==0:
            return eds
        ma = N.max(v[:eidx]) # max of the region
        th2 = ma*self.covratio
        #acovth = max(th1,th2)
        #acovth=th2
        #self._acovth = acovth
        while(eidx>0 and sm[eidx]<th2):
            eidx = eidx-1
        while(eidx>0 and v[eidx]<th2):
            eidx = eidx-1
        return [x for x in eds[:-1] if x<eidx]+[eidx]

    def detect_min(self, v):
        idx = N.argmin(v)
        l = len(v)
        mis = self.minintsize
        if (idx<mis) or (l-idx<mis) :
            return []
        return [idx]

    def detect_low_level(self, v):
        ma = N.max(v)
        l = len(v)
        mis = self.minintsize
        th1 = self.covth # absolute min
        th2 = ma*self.covratio
        th3 = 2*th2
        idx = N.nonzero(v<th1)[0]
        def _chk(idx):
           return (len(idx)==0) or (idx[0]<mis) or (l-idx[0]<mis) 
        if _chk(idx):
            idx = N.nonzero(v<th2)[0]
            if _chk(idx):
                idx = N.nonzero(v<th3)[0]
                if _chk(idx):
                    return []
        return [idx[0]]

    def detect_rise(self, v):
        ma = N.max(v)
        mima = N.min(v)/ma
        if mima<self.mimath:
            th1 = ma*self.mimath
            th2 = th1*self.triggerth
        elif mima<self.mimath2:
            th1 = ma*self.mimath2
            th2 = th1+(ma*self.mimath)
        else:
            return []
        ist = N.nonzero(v<th1)[0][0]
        mis = self.minintsize
        if (ist<mis) or (len(v)-ist<mis):
            return []
        #LOG.debug('detect_rise startidx:{0}'.format(ist))
        cmin = v[ist]
        imin = ist
        for i in range(ist+1,len(v)):
            if cmin>v[i]:
                cmin = v[i]
                imin = i
            elif v[i]>th2: # rise detected
                #LOG.debug(' rise at:{0},trigger at:{1}'.format(imin, i))
                return [imin]
        return []

    def detect_rise2(self, v):
        ma = N.max(v)
        mi = N.min(v)
        mima = mi/ma
        if mima<self.mimath:
            th1 = ma*self.mimath
            th2 = th1*self.triggerth
        elif mima<self.mimath2:
            th1 = ma*self.mimath2
            th2 = th1+(ma*self.mimath)
        else:
            return []
        ist = N.nonzero(v>=th1)[0][0] # <=== different from detect_rise
        mis = self.minintsize
        if (ist<mis) or (len(v)-ist<mis):
            return []
        #LOG.debug('detect_rise startidx:{0}'.format(ist))
        cmin = v[ist]
        imin = ist
        for i in range(ist+1,len(v)):
            if cmin>v[i]:
                cmin = v[i]
                imin = i
            elif v[i]>th2: # rise detected
                #LOG.debug(' rise at:{0},trigger at:{1}'.format(imin, i))
                return [imin]
        return []

    def detect_sharp_drops(self, v, maxlen):
        ws = self.winsize # smooth window for derivative
        hws = int(ws/2) # ws has to be odd number
        win = self.win 
        #lv = N.log2(v+1)
        lv = v
        lv1 = N.concatenate([win*lv[0], lv, win*lv[-1]])
        sm = N.convolve(lv1, win/float(ws), 'same')
        dm = (sm[ws:]-sm[:-ws])[(hws+1):-hws]
        sigma = dm.std()
        th = max(self.minth, self.sigmath*sigma)
        bs = self.findpeak(dm, th, -1) # detect drop
        return self._aggregate(maxlen,bs)

    def findpeak(self, dtmp, th, sign):
        if sign ==-1: # negative peak
            idx = N.nonzero(dtmp<-th)[0]
        else:
            idx = N.nonzero(dtmp>th)[0]
        # if non found at this stage return []
        #LOG.debug 'len(idx)=',len(idx)
        if len(idx)==0:
            return []
        # group into contiguous indices
        groups = []
        x0 = idx[0]
        cgr = [x0]
        for x in idx[1:]:
            if x==(x0+1): # next index
                cgr.append(x)
                x0=x
            else:
                groups.append(N.array(cgr))
                x0 = x
                cgr = [x0]
        groups.append(cgr) # last group
        #LOG.debug 'groups=',groups
        # find min(sign -1) or max(sign +1) within each group
        if sign == -1:
            rslt = [g[N.argmin(dtmp[g])] for g in groups]
        else:
            rslt = [g[N.argmax(dtmp[g])] for g in groups]
        return rslt

    def _aggregate(self, maxlen, bs):
        if len(bs)==0:
            return []
        bs = sorted(bs)
        mis = self.minintsize
        while(len(bs)>0 and bs[0]<mis):
            bs = bs[1:]
        while(len(bs)>0 and maxlen-bs[-1]<mis):
            bs = bs[:-1]
        if len(bs)>1:
            bsi = bs[::-1]
            ar = self.aggregateratio # 10% of longer
            cur = bsi[0]
            mis2 = ar*cur
            bs2 = [cur]
            for b in bsi[1:]:
                if (cur-b)>mis2:#ar*cur:
                    bs2.append(b)
                    cur = b
            bs = sorted(bs2)
        # if len(bs)>1: # aggregate close by
        #     bs = [bs[0]] + [x1 for x0,x1 in zip(bs[:-1], bs[1:]) if x1-x0>mis]
        return bs        

####### Colors  ########################################################################
# move to plotutil

import matplotlib.colors as C
import matplotlib.cm as CM

class Colors(object):
    
    def __init__(self, mapname, vmax, vmin=0, nl=32):
        self.mn = mapname
        self.vmin = vmin
        self.vmax = vmax
        self.d = d = 1./nl
        if mapname=='C':
            self.rgba = [(1.-x,1.,1.,1.) for x in N.arange(0,1+d,d)]
        elif mapname=='M':
            self.rgba = [(1.,1.-x,1.,1.) for x in N.arange(0,1+d,d)]
        elif mapname=='Y':
            self.rgba = [(1.,1.,1.-x,1.) for x in N.arange(0,1+d,d)]
        elif mapname=='R':
            self.rgba = [(1.,1.-x,1.-x,1.) for x in N.arange(0,1+d,d)]
        elif mapname=='G':
            self.rgba = [(1.-x,1.,1.-x,1.) for x in N.arange(0,1+d,d)]
        elif mapname=='B':
            self.rgba = [(1.-x,1.-x,1.,1.) for x in N.arange(0,1+d,d)]
        else:
            cm = P.get_cmap(mapname)
            cnorm = C.Normalize(vmin=0,vmax=1.)
            self.sm = sm = CM.ScalarMappable(norm=cnorm,cmap=cm)
            self.rgba = [sm.to_rgba(x) for x in N.arange(0,1+d,d)]
            
    def to_rgba(self, v):
        d = self.d
        if self.mn in ['R','G','B','C','M','Y']:
            vn = max(0., (float(v)-self.vmin)/self.vmax)
            vn = min(1., vn)
            vni = int(vn/d)
            return self.rgba[vni]
        return self.sm.to_rgba(float(v))
    
    def RGB(self, v):
        # to RGB string (e.g. 255,0,0)
        rgba = [str(int(255*x)) for x in self.to_rgba(v)][:3]
        return ','.join(rgba)
        
####### Local Assembler ###############################################################
def detect_exons(sjpaths, offset, sja, exa, classifier=INTG):
    x = N.log2(sja+1)
    xd = (x[1:]-x[:-1])
    # use sjpaths to get donor/acceptor positions
    idxp = N.nonzero(xd>4)[0]+1 # donor(+ strand), acceptor(- strand)
    idxn = N.nonzero(xd<-4)[0]+1 # acceptor(+ strand), donor(- strand)
    tmp = [[x,'p',0] for x in idxp]+[[x,'n',0] for x in idxn]
    gaps0 = find_np_pairs(tmp, maxsize=10000)
    # tst: donor (+ strand), acceptor (- strand) => idxp
    # ted: => idxn
    idxp = set(sjpaths['tst'].values-offset)
    idxn = set(sjpaths['ted'].values-offset)
    tmp = [[x,'p',0] for x in idxp]+[[x,'n',0] for x in idxn]
    # gaps = find_np_pairs(tmp, xd)
    gaps1 = find_np_pairs(tmp)
    gaps = sorted(set(gaps0+gaps1))
    zoom = classifier.json['zoom']
    covfactor = classifier.json['th']
    def _gen_params():
        #find_maxgap = cyas2.find_maxgap
        for st,ed in gaps:
            # lemax, lgap, llen
            exsub = exa[st:ed]
            if len(exsub)==0:
                print(st,ed)
            emax = exsub.max()
            th = emax*covfactor
            lemax = N.log2(zoom*emax+1)
            lgap = N.log10(find_maxgap2(exsub, th)+1)
            llen = N.log10(ed-st+1)
            sdmax = max(xd[st-1],xd[ed-1])
            mp = float(N.sum(exsub>th))/len(exsub)
            yield (st,ed,lemax,lgap,llen,sdmax,mp)
    cols = ['ost','oed','lemax','lgap','llen','sdmax','mp']
    df = PD.DataFrame([x for x in _gen_params()], columns=cols)
    classifier.classify(df)
    return df
    
def find_np_pairs(tmp, maxsize=20000):
    # tmp=[ [pos, 'n' or 'p', 0], ...]
    # find n=>p pairs
    tmp = sorted(tmp)
    n = len(tmp)
    def _gen_sted():
        for i in range(n):
            x = tmp[i]
            if x[1]=='n':
                for j in range(i+1,n):
                    y = tmp[j]
                    if (y[0]-x[0]>maxsize):
                        break
                    if (y[1]=='p')&(y[0]>x[0]):
                        x[2],y[2]=1,1
                        yield (x[0],y[0])
                        break
        for j in range(n):
            y = tmp[j]
            if (y[1]=='p')&(y[2]==0): # unused
                for i in range(j-1,-1,-1):
                    x = tmp[i]
                    if (y[0]-x[0]>maxsize):
                        break
                    if (x[1]=='n')&(x[0]<y[0]):
                        x[2],y[2]=1,1
                        yield (x[0],y[0])
                        break
    return [x for x in _gen_sted()]

def find_maxgap2(arr, th):
    idx = N.nonzero(arr<=th)[0]
    if len(idx)==0:
        return 0
    dif = idx[1:]-idx[:-1] # if continuous dif==1
    idx2 = N.nonzero(dif>1)[0] # non contiguous   
    idxst = N.array([idx[0]]+list(idx[idx2+1]))
    idxed = N.array(list(idx[idx2])+[idx[-1]])
    # gap start idx2[x]+1 ==> gap end idx2[x+1]
    gsize = idxed - idxst + 1
    return N.max(gsize)
    
def unionregion(df, sfld='st', efld='ed'):
    if type(df)==type([]):
        sted = sorted(df)
    else:
        sted = df.sort_values([sfld,efld])[[sfld,efld]].values
    def _gen():
        rec0 = sted[0]
        for rec1 in sted[1:]:
            if rec0[1] < rec1[0]: # ed1<st0 new interval
                yield rec0
                rec0 = rec1
            else: # overlapping/contiguous
                rec0[1] = max(rec0[1], rec1[1]) # update the end
        yield rec0
    recs = [x for x in _gen()]
    return recs
    
def fill_gap(sja, sj, exons, strand, offset):
    sjac = sja.copy()
    # need to fill exons inside the path as well
    if strand =='+':
        steds = sorted(set([tuple([int(z) for z in x.split(',')])  \
                                for y in sj['name'] 
                                for x in y.split('|')[1:-1]]))
    else:
        steds = sorted(set([tuple([int(z) for z in x.split(',')][::-1])  \
                                for y in sj['name'] 
                                for x in y.split('|')[1:-1]]))

    for st,ed in steds: # this way both 5' and 3' positions will be correct
        ost = st-offset
        oed = ed-offset
        sjac[ost:oed] = min(sjac[ost-1],sjac[oed+1])

    if len(exons)==0:
        return sjac
    #gaps0 = gaps[gaps['exon']==True]
    gaps1 = unionregion(exons, 'ost', 'oed')
    # if strand=='+': # >>> fill from right
    #     for st,ed in gaps1:
    #         sjac[st:ed] = sjac[st-1]
    # else: # <<< fill from left
    #     for st,ed in gaps1:
    #         sjac[st:ed] = sjac[ed+1]
    # return sjac
    # above only 5' positions are correct (3' are offset by exon length)
    for st,ed in gaps1: # this way both 5' and 3' positions will be correct
        sjac[st:ed] = min(sjac[st-1],sjac[ed+1])
    return sjac
    
def _pc(st,ed,strand,sep):
    if strand in ['+','.+','.']:
        return '{0}{2}{1}'.format(int(st),int(ed),sep)
    return '{1}{2}{0}'.format(int(st),int(ed),sep)

def set_ad_pos(df, which='sj'):
    if which == 'sj':
        idx = df['strand'].isin(['-','.-'])
    else:
        idx = df['strand'].isin(['+','.+'])
    df.loc[idx, 'dpos'] = df[idx]['ed']
    df.loc[~idx,'dpos'] = df[~idx]['st']
    df.loc[idx, 'apos'] = df[idx]['st']
    df.loc[~idx,'apos'] = df[~idx]['ed']
    df['dpos'] = df['dpos'].astype(int)
    df['apos'] = df['apos'].astype(int)

def detect_53(sja, exa, strand, classifier=E53C):
    zoom = classifier.json['zoom']
    if strand=='+':
        x = N.log2(zoom*sja+1)
    else:
        x = N.log2(zoom*sja[::-1]+1)
    xd = (x[1:]-x[:-1])
    xm = (x[1:]+x[:-1])/2.
    sdiffth = classifier.json['sdiffth']
    idxp = N.nonzero(xd>sdiffth)[0] # source
    idxn = N.nonzero(xd<-sdiffth)[0] # sink
    # pos, sdiff, smean, kind
    n = len(x)
    if strand=='+':
        recs = [(i+1, N.abs(xd[i]), xm[i], '5') for i in idxp]+\
               [(i+1, N.abs(xd[i]), xm[i], '3') for i in idxn]
    else:
        recs = [(n-i-1, N.abs(xd[i]), xm[i], '5') for i in idxp]+\
               [(n-i-1, N.abs(xd[i]), xm[i], '3') for i in idxn]
        # position is off by one for negative strand (since xd is calculated in + strand)
    df = PD.DataFrame(recs, columns=['pos','sdiff','smean','kind'])
    classifier.classify(df)
    df = df[df['e53']==True].copy()
    return df

def reversepathcode(pc):
    return ','.join(['|'.join(x.split('|')[::-1]) for x in pc.split(',')][::-1])

AFLD = {'ex':{'+':'st','-':'ed','.':'st'},
        'sj':{'+':'ed','-':'st','.':'ed'}}
DFLD = {'ex':{'+':'ed','-':'st','.':'ed'},
        'sj':{'+':'st','-':'ed','.':'st'}}
STRS = {'+':['+','.+','.'],
        '-':['-','.-','.'],
        '.':['.+','.-','.']}
EXDFCOLS = ['chr','st','ed','strand','name','kind','ecov']
SJDFCOLS = ['chr','st','ed','strand','name','kind','tcnt','ucnt' ]#,'donor','acceptor','dp','ap']
PATHCOLS = ['chr','st','ed','name','strand','tst','ted','tcov','tcov0','tcov0a','tcov0b','tcov0c']

LAPARAMS = dict(
     refcode='gen9',
     discardunstranded=False,
     uth=1, 
     mth=3, 
     sjratioth=2e-3, 
     usjratioth=1e-2,
     #covfactor=0.05, 
     tcovth=1,
     upperpathnum=100, # if num of paths larger than this increase stringency for sjs
     pathcheckth=100, # above this num of sjs check sc1(ucnt)==0 if >50% remove
     pathcheckratio=0.2, # ratio of ucnt==0 if above this remove these
     use_ef2=False, # whether to use slope edge detector
     mixunstranded=True,
     maxexonsize=30000, 
     edgedelta=10,
     minsearchsize=500,
)

class LocalAssembler(object):

    def __init__(self, 
        bwpre, 
        chrom, st, ed, 
        dstpre, 
        classifierpre=None, 
        sjbwpre=None,
        sjpaths=None, 
        **kw):

        self.bname = '{0}:{1}-{2}'.format(chrom,st,ed)
        self.bwpre = bwpre
        self.sjbwpre = sjbwpre
        self.dstpre = dstpre
        self.chrom = chrom
        self.st = int(st)
        self.ed = int(ed)
        self.classifierpre=classifierpre
        self.params = LAPARAMS.copy()
        self.params.update(kw)
        mixunstranded = not self.params['discardunstranded']
        self.sjexbw = sjexbw = SjExBigWigs(bwpre, sjbwpre, mixunstranded=mixunstranded)
        self.arrs = arrs = {}
        with sjexbw: # get bw arrays
            for k in ['ex','sj']:
                arrs[k] = {}
                for s in ['+','-']:
                    arrs[k][s] = sjexbw.bws[k][s].get(chrom, st, ed)
        self._sjpaths=sjpaths
        self.load_classifiers()

    def process(self):
        self.load_sjpaths() # ._sjpaths0        
        self.distribute_ustrand() # .sjpaths0
        self.filter_sjpaths() # .sjpaths
        if len(self.sjpaths)==0:
            return None

        self.logdebug('finding exons...')
        self.find_exons() 
        self.logdebug('finding 53 pos...')
        self.find_53pos() 
        self.logdebug('making dataframes...')
        self.make_sjexdf()
        self.logdebug('finding 53 exons...')
        self.find_53edges()

        self.logdebug('calculating covrages...')
        self.calculate_scovs()
        self.calculate_ecovs()
        self.logdebug('finding paths...')
        self.find_groups() 

        self.logdebug('writing results...')
        self.write()

        self.loginfo('finished assembling, {0} paths found'.format(len(self.paths)))
        if len(self.paths)>0:
            return self.bname
        return None


    def __str__(self):
        return 'LocalAssembler({0}:{1}-{2}, bwpre:{3}, dstpre:{4}'.format(self.chrom,self.st,self.ed,self.bwpre,self.dstpre)

    def _log(self, msg, level='debug'):
        _l = getattr(LOG, level)
        _l('{0}: {1}'.format(self.bname, msg))

    def loginfo(self, msg):
        self._log(msg, 'info')

    def logdebug(self, msg):
        self._log(msg, 'debug')
        
    def load_classifiers(self):
        pathpre = self.classifierpre
        if (pathpre is None):
            self.intg = INTG
            self.e53c = E53C 
            self.ef5 = EdgeFinder(EF5JSON, self.params['use_ef2'])
            self.ef3 = EdgeFinder(EF3JSON, self.params['use_ef2'])
            return

        path = pathpre+'.exonparams.json'
        if os.path.exists(path):
            with open(path,'r') as fp:
                self.exonparams = ep = json.load(fp)
            self.intg = LogisticClassifier(json=ep, dstcol='exon')        
        else:
            LOG.warning('{0} does not exists, reverting to default'.format(path))
            self.intg = INTG

        path = pathpre+'.gap5params.json'
        if os.path.exists(path):        
            with open(path,'r') as fp:
                self.gap5params = g5p = json.load(fp)        
            self.ef5 = EdgeFinder(g5p,self.params['use_ef2'])
        else:
            LOG.warning('{0} does not exists, reverting to default'.format(path))
            self.ef5 = EdgeFinder(EF5JSON, self.params['use_ef2']) 
        path = pathpre+'.gap3params.json'   
        if os.path.exists(path):        
            with open(path,'r') as fp:
                self.gap3params = g3p = json.load(fp)        
            self.ef3 = EdgeFinder(g3p,self.params['use_ef2'])
        else:
            LOG.warning('{0} does not exists, reverting to default'.format(path))
            self.ef3 = EdgeFinder(EF3JSON, self.params['use_ef2']) 

        if self.sjbwpre is not None:
            pathpre = self.sjbwpre+'.'+refcode
        path = pathpre+'.e53params.json'
        if os.path.exists(path):                
            with open(path,'r') as fp:
                self.e53params = e5p = json.load(fp)    
            self.e53c = LogisticClassifier(json=e5p, dstcol='e53')
        else:
            LOG.warning('{0} does not exists, reverting to default'.format(path))
            self.e53c = E53C

    def load_sjpaths(self):
        self._sjpaths0 = sjpaths0 = self._sjpaths
        if sjpaths0 is not None:
            return

        chrom,st,ed = self.chrom,self.st,self.ed
        # single bwpre
        if UT.isstring(self.bwpre):
            chrfiltered = self.bwpre+'.sjpath.{0}.filtered.bed.gz'.format(chrom)
            if os.path.exists(chrfiltered):
                sj = GGB.read_bed(chrfiltered)
            else:
                chrpath = self.bwpre+'.sjpath.{0}.bed.gz'.format(chrom) # separated chrom file exists?
                if os.path.exists(chrpath):
                    sj = GGB.read_bed(chrpath)
                else:
                    sj = GGB.read_bed(self.bwpre+'.sjpath.bed.gz')
            idx0 = (sj['chr']==chrom)&(sj['tst']>=st)&(sj['ted']<=ed)        
            sj0 = sj[idx0].copy()
            # merged sjpath has 53exon in pathcode => remove
            if len(sj0)>0:
                name0 = sj0.iloc[0]['name']
                if len(name0.split('|'))<len(name0.split(',')):
                    sj0['pathcode'] = sj0['name']
                    sj0['name'] = [','.join(x.split(',')[1:-1]) for x in sj0['name']]
                else:
                    idxp = sj0['strand'].isin(['+','.'])
                    sj0.loc[idxp,'pathcode'] = ['{0},{1},{2}'.format(st,n,ed) for st,n,ed in sj0[idxp][['st','name','ed']].values]
                    sj0.loc[~idxp,'pathcode'] = ['{2},{1},{0}'.format(st,n,ed) for st,n,ed in sj0[~idxp][['st','name','ed']].values]
            self._sjpaths0 = sj0
            return 

        # list of bwpres, load and merge
        sjps0 = [GGB.read_bed(b+'.sjpath.bed.gz') for b in self.bwpre]
        sjps = []
        for sj in sjps0:
            idx0 = (sj['chr']==chrom)&(sj['tst']>=st)&(sj['ted']<=ed)        
            sj0 = sj[idx0].copy()
            if len(sj0)>0:
                name0 = sj0.iloc[0]['name']
                if len(name0.split('|'))<len(name0.split(',')):
                    sj0['name'] = [','.join(x.split(',')[1:-1]) for x in sj0['name']]            
            sjps.append(sj0)
        sjp = PD.concat(sjps, ignore_index=True)
        sjg = sjp.groupby(['chr','name'])
        sj = sjg.first()
        # chr,st,ed,name,sc1,strand,tst,ted,sc2,#exons,esizes,estarts
        sj['st'] = sjg['st'].min().astype(int)
        sj['ed'] = sjg['ed'].max().astype(int)
        sj['sc1'] = sjg['sc1'].sum()
        sj['sc2'] = sjg['sc2'].sum()
        self._sjps = sjps 
        sj = sj.reset_index()        
        self._sjpaths0 = sj

    def distribute_ustrand(self):
        bwpre = self.bwpre
        chrom,st,ed = self.chrom,self.st,self.ed

        s0 = self._sjpaths0
        idxu = s0['strand']=='.' # unstranded => duplicates into '.+','.-'
        if self.params['discardunstranded']:
            s1 = s0[~idxu].copy()
        else:
            if N.sum(idxu)>0:
                # if unstranded junction share donor/acceptor sites with stranded one then assign that strand
                sj_pn = s0[~idxu]
                if len(sj_pn)>0:
                    tst2str = UT.df2dict(sj_pn, 'tst', 'strand')
                    ted2str = UT.df2dict(sj_pn, 'ted', 'strand')
                    sj_u = s0[idxu].copy()
                    sj_u['strand'] = [tst2str.get(x,y) for x,y in sj_u[['tst','strand']].values]
                    sj_u['strand'] = [ted2str.get(x,y) for x,y in sj_u[['ted','strand']].values]
                    idx_n = sj_u['strand']=='-'
                    sj_u.loc[idx_n, 'name'] = [reversepathcode(x) for x in sj_u[idx_n]['name']]
                    sj_u.loc[idx_n, 'pathcode'] = [reversepathcode(x) for x in sj_u[idx_n]['pathcode']]
                    idxu2 = sj_u['strand']=='.'
                    sj_upn = sj_u[~idxu2]
                    self.logdebug('{0} unstranded assigned +/- with matching ends'.format(len(sj_upn)))
                    sj_up = sj_u[idxu2].copy()
                    sj_un = sj_u[idxu2].copy()
                else:
                    sj_upn = None
                    sj_up = s0[idxu].copy()
                    sj_un = s0[idxu].copy()

                sj_up['strand'] = '.+'
                sj_un['name'] = [reversepathcode(x) for x in sj_un['name']]
                sj_un['pathcode'] = [reversepathcode(x) for x in sj_un['pathcode']]
                sj_un['strand'] = '.-'
                s1 = PD.concat([sj_pn, sj_upn, sj_up, sj_un], ignore_index=True)
            else:
                s1 = s0
        self.sjpaths0 = s1

    def filter_sjpaths(self):
        uth,mth = self.params['uth'],self.params['mth']
        sjratioth = self.params['sjratioth']
        usjratioth = self.params['usjratioth']
        chrom,st,ed = self.chrom,self.st,self.ed
        
        sc1 = self.sjpaths0['sc1']
        if self.sjpaths0.dtypes['sc2']=='O': # merged sjpath
            self.sjpaths0['sc2'] = sc1
        sc2 = self.sjpaths0['sc2']
        idx1 = (sc1>=uth)|(sc2-sc1>=mth)
        self.sjpaths = sjpaths = self.sjpaths0[idx1].copy()
        # max ratio to cov (sj+ex) > sjratioth
        with self.sjexbw:
            sjaa = self.sjexbw.bws['sj']['a'].get(chrom, st, ed)
            exaa = self.sjexbw.bws['ex']['a'].get(chrom, st, ed)
        a = sjaa+exaa # all of the coverages
        o = int(self.st)
        # sjpaths['minscov'] = [N.min(a[s-o:e-o]) for s,e in sjpaths[['tst','ted']].values]]
        sjpaths['sjratio'] = [x/N.min(a[int(s-o):int(e-o)]) for x,s,e in sjpaths[['sc2','tst','ted']].values]
        sjpaths['sjratio2'] = [x/N.mean(a[int(s-o):int(e-o)]) for x,s,e in sjpaths[['sc1','tst','ted']].values]
        # .values => dtype float matrix => s,e float
        n0 = len(sjpaths)
        idxpn = (sjpaths['strand'].isin(['+','-']))&(sjpaths['sjratio2']>sjratioth)
        idxu = (sjpaths['strand'].isin(['.+','.-']))&(sjpaths['sjratio2']>usjratioth)
        self.sjpaths = sjpaths[idxpn|idxu].copy()
        n1 = len(self.sjpaths)
        self.loginfo('sjratio filter: {0}=>{1}'.format(n0,n1))

    def find_exons(self):
        arrs = self.arrs
        self.filled  = {}
        self.exons  = {}
        self.gaps = {}
        sjs = self.sjpaths
        for s in ['+','-']:
            sja = arrs['sj'][s]
            exa = arrs['ex'][s]
            sj = sjs[sjs['strand'].isin(STRS[s])]
            df = detect_exons(sj, self.st, sja, exa, classifier=self.intg)
            self.exons[s] = df[df['exon']==True].copy()            
            self.gaps[s] = df
            self.filled[s] = fill_gap(sja, sj, self.exons[s], s, self.st)

    def find_53pos(self):
        # edge case
        # internal case
        self.e53pos = {}
        for s in ['+','-']:
            sja = self.filled[s]
            exa = self.arrs['ex'][s]            
            self.e53pos[s] = detect_53(sja, exa, s, classifier=self.e53c)

    def make_sjexdf(self):
        ap = self.sjpaths
        dfs = []
        dfe = []
        o = self.st
        chrom = self.chrom
        def _sgen(): # junctions from sjpaths
            sted = set()
            for strand in ['+','.+', '-','.-']:
                for p in ap[ap['strand']==strand]['name'].values:
                    for x in p.split(','):
                        st,ed = [int(y) for y in x.split('|')]
                        if st>ed:
                            st,ed = ed,st
                        if (st,ed,strand[-1]) not in sted:
                            yield (chrom,st,ed,strand,x,'j')
                            sted.add((st,ed,strand[-1]))
        def _egen1(): # internal exons from sjpaths
            sted = set()
            for strand in ['+','.+', '-','.-']:
                for p in ap[ap['strand']==strand]['name'].values:
                    for x in p.split('|')[1:-1]:
                        st,ed = [int(y) for y in x.split(',')]
                        if st>ed:
                            st,ed = ed,st
                        if (st,ed,strand[-1]) not in sted:
                            yield (chrom,st,ed,strand,x,'i')
                            sted.add((st,ed,strand[-1]))
        cols = ['chr','st','ed','strand','name','kind']
        sjdf = PD.DataFrame([x for x in _sgen()], columns=cols)
        exdfi1 = PD.DataFrame([x for x in _egen1()], columns=cols)
        # sted = set([tuple(x) for x in exdfi1[['st','ed','strand']].values])
        sted = set([(x,y,z[-1]) for x,y,z in exdfi1[['st','ed','strand']].values])
        def _egen2(): # internal exons from adjacent junctions        
            for strand in ['+','-']: # connected exons
                ex = self.exons[strand]
                for ost,oed in ex[['ost','oed']].values:
                    st = ost+o
                    ed = oed+o
                    if (st,ed,strand) not in sted:
                        yield (chrom,st,ed,strand,_pc(st,ed,strand,','),'i')
                        sted.add((st,ed,strand))
        exdfi2 = PD.DataFrame([x for x in _egen2()], columns=cols)
        exdfi = PD.concat([exdfi1, exdfi2], ignore_index=True)
        # other 53 exons: edges of sjpaths not already in exdfi
        # st => ed, ed => st
        set_ad_pos(sjdf, 'sj')
        set_ad_pos(exdfi, 'ex')
        exdfi['len'] = exdfi['ed'] - exdfi['st']
        exdfi.sort_values('len',inplace=True)
        a2len = UT.df2dict(exdfi, 'apos', 'len')
        d2len = UT.df2dict(exdfi, 'dpos', 'len')
        delta = self.params['edgedelta']
        def _e53gen1(): # 
            adpos = set()
            for strand in ['+','.+', '-','.-']:
                for pc in ap[ap['strand']==strand]['pathcode'].values:
                    tmp = pc.split('|')
                    e5 = tmp[0]
                    apos,dpos = [int(y) for y in e5.split(',')]
                    st,ed = min(apos,dpos),max(apos,dpos)
                    if (dpos, strand[-1]) not in adpos:
                        if (dpos not in d2len) or (N.abs(apos-dpos)>(d2len[dpos]+delta)):
                            # print('dpos', strand, dpos, pc)
                            yield (chrom,st,ed,strand,e5,'5')
                            adpos.add((dpos,strand[-1]))
                    e3 = tmp[-1]
                    apos,dpos = [int(y) for y in e3.split(',')]
                    st,ed = min(apos,dpos),max(apos,dpos)
                    if (apos, strand[-1]) not in adpos:
                        if (apos not in a2len) or (N.abs(apos-dpos)>(a2len[apos]+delta)):
                            # print('apos', strand,apos, pc)
                            yield (chrom,st,ed,strand,e3,'3')
                            adpos.add((apos,strand[-1]))
        e53df1 = PD.DataFrame([x for x in _e53gen1()], columns=cols)
        e53df1['origin'] = 'path'
        set_ad_pos(e53df1, 'ex')
        e5set = set([(x,y[-1],z) for x,y,z in e53df1[['dpos','strand','kind']].values])
        e3set = set([(x,y[-1],z) for x,y,z in e53df1[['apos','strand','kind']].values])
        def _e53gen2():
            for strand in ['+','-']:
                e53 = self.e53pos[strand]
                for pos,k in e53[['pos','kind']].values:
                    pos1 = pos+o
                    if k=='5':
                        if (pos1,strand,k) not in e5set:
                            yield (chrom,pos1,pos1,strand,_pc(pos1,pos1,strand,','),k)
                    else:
                        if (pos1,strand,k) not in e3set:
                            yield (chrom,pos1,pos1,strand,_pc(pos1,pos1,strand,','),k)

        e53df2 = PD.DataFrame([x for x in _e53gen2()], columns=cols)
        e53df2['origin'] = 'flow'
        set_ad_pos(e53df2)
        c2 = cols+['apos','dpos']
        c3 = c2 + ['origin']
        e53df = PD.concat([e53df1[c3], e53df2[c3]], ignore_index=True)
        # sjdf = sjdf.groupby(['chr','st','ed','strand']).first().reset_index()
        # exdfi = exdfi.groupby(['chr','st','ed','strand']).first().reset_index()
        # e53df = e53df.groupby(['chr','st','ed','strand','kind']).first().reset_index()
        self.sjdf = sjdf
        self.exdfi = exdfi
        self.e53df = e53df
        self.exdf = PD.concat([exdfi[c2], e53df[c2]], ignore_index=True)

    def _get_spans(self, strand, recalc=False):
        if hasattr(self, '_spans') and not recalc:
            return self._spans[strand]
        sj0 = self.sjdf
        ex0 = self.exdf
        self._spans={}
        for s in ['+','-']:
            sj0s = sj0[sj0['strand'].isin(STRS[s])]
            ex0s = ex0[ex0['strand'].isin(STRS[s])]
            o = self.st
            sj1 = [[st,ed] for st,ed in sj0s[['st','ed']].values]
            ex1 = [[st,ed] for st,ed in ex0s[['st','ed']].values]
            arr = sj1+ex1
            if len(arr)==0:
                self._spans[s] = []
            else:
                self._spans[s] = UT.union_contiguous_intervals(arr)
        return self._spans[strand]

    def _find_pos0(self, pos, strand, direction):
        gs = N.array(self._get_spans(strand))
        o = self.st
        if direction=='<': # look for last gspan end
            tgt = gs[:,1]
            i0 = bisect.bisect_left(tgt, pos+o)
            if i0==0:
                p0 = 1
            else:
                p0 = tgt[i0-1]-o
            p0 = min(max(p0, pos-self.params['maxexonsize']), pos-self.params['minsearchsize'])
            return max(p0, 1)
        tgt = gs[:,0] # look for next gspan start
        i0 = bisect.bisect_right(tgt, pos+o)
        if i0==len(tgt):
            p0 = len(self.arrs['sj'][strand])-1
        else:
            p0 = tgt[i0]-o
        p0 =  max(min(p0, pos+self.params['maxexonsize']), pos+self.params['minsearchsize'])
        return min(p0, len(self.arrs['sj'][strand])-1)

    def _subtract_exons(self, pos, pos0, sja, exa, exs, direction):
        # direction <
        # find exons between pos0=>pos, subtract sja corresponding to further
        # edge of the exon (at position st)
        o = self.st
        if direction=='<':
            sja1 = sja[pos0-1:pos+1].copy() # index offset by 1
            exa1 = exa[pos0:pos+1].copy()
            ex = exs[(exs['st']>=pos0+o)&(exs['ed']<pos+1+o)]
            for st,ed in ex[['st','ed']].values:
                st0 = st-o-pos0
                ed0 = ed-o-pos0
                exa1[st0:ed0] = exa1[st0:ed0]-(sja1[st0]-sja1[st0+1]) # st0-1 but array itself is offset by 1
            return sja1[1:], exa1 # same length
        # direction >
        # find exons between pos=>pos0, subtract sja corresponding to further
        # edge of the exon (at position ed)
        sja1 = sja[pos-1:pos0+2].copy() # same index but get one more on right
        exa1 = exa[pos-1:pos0].copy()
        ex = exs[(exs['st']>=pos-1+o)&(exs['ed']<pos0+o)] # don't include = for ed
        for st,ed in ex[['st','ed']].values:
            st0 = st-o-pos+1
            ed0 = ed-o-pos+1
            exa1[st0:ed0] = exa1[st0:ed0]-(sja1[ed0+1]-sja1[ed0])
        return sja1[:len(exa1)], exa1 # same length

    def find_53edges(self):
        exdfi = self.exdfi
        e53df = self.e53df
        EF = {'5':self.ef5, '3':self.ef3}
        KIND = {'<':{'+':'5','-':'3'},'>':{'+':'3','-':'5'}}
        DIREC = {'+':{'5':'<','3':'>'},'-':{'5':'>','3':'<'}}
        POS = {'+':{'5':'ed','3':'st'},'-':{'5':'st','3':'ed'}}
        SWAP = {'+':{'5':True,'3':False},'-':{'5':False,'3':True}}
        cols = ['chr','st','ed','strand','name','kind']
        c2 = cols+['apos','dpos']
        chrom = self.chrom
        o = self.st
        def _gen():
            for strand in ['+','-']:
                sja = self.arrs['sj'][strand]
                exa = self.arrs['ex'][strand]
                for kind in ['5','3']: 
                    exs = e53df[(e53df['strand'].isin(STRS[strand]))&(e53df['kind']==kind)]
                    direction = DIREC[strand][kind]
                    for pos1 in exs[POS[strand][kind]].values:
                        pos = pos1-o
                        pos0 = self._find_pos0(pos, strand, direction)
                        sja1, exa1 = self._subtract_exons(pos, pos0, sja, exa, exs, direction)
                        eposs = EF[kind].find(sja1,exa1,direction)
                        if len(eposs)==0:
                            LOG.warning('no edge found {0}:{1}:{2}:{3}'.format(chrom,pos1,strand,kind))
                        for epos in eposs:
                            st = pos1
                            ed = epos+pos1
                            if SWAP[strand][kind]:
                                st,ed=ed,st
                            if st==ed: # ignore st==ed
                                LOG.warning('edge not found (st==ed) {0}:{1}:{2}:{3}'.format(chrom,pos1,strand,kind))
                            elif st>ed:
                                LOG.warning('edge wrong direction {0}:{1}:{2}:{3}'.format(chrom,pos1,strand,kind))
                            else: # st<ed
                                name = _pc(st,ed,strand,',')
                                yield (chrom,st,ed,strand,name,kind)
        e53fixed = PD.DataFrame([x for x in _gen()], columns=cols)
        n0 = len(e53fixed)
        e53fixed = e53fixed[e53fixed['st']!=e53fixed['ed']]
        n1 = len(e53fixed)
        if n0!=n1:
            LOG.warning('#st==ed:{0}'.format(n0-n1))
        set_ad_pos(e53fixed, 'ex')
        self.e53fixed = e53fixed
        self.exdf = PD.concat([exdfi[c2], e53fixed[c2]], ignore_index=True)
        self._get_spans('+', recalc=True)
        
    def calculate_scovs(self):
        sj = self.sjdf
        sj0 = self.sjpaths0
        sj0mat = sj0[['sc1','sc2','name']].values
        tmp = [[(sc1,sc2) for sc1,sc2,p in sj0mat if y in p] for y in sj['name']]
        sj['ucnt'] = [N.sum([x[0] for x in y]) for y in tmp]
        sj['tcnt'] = [N.sum([x[1] for x in y]) for y in tmp]
        # idx = sj['tcnt']==0
        # tmp0 = ['{1}|{0}'.format(*y.split('|')) for y in sj[idx]['name']]
        # tmp1 = [N.sum([x for x,p in sj0mat if y in p]) for y in tmp0]
        # sj.loc[idx, 'tcnt'] = tmp1
        idxz = sj['tcnt']==0
        if N.sum(idxz)>0:
            sj.loc[idxz,'tcnt'] = 1e-6

    def calculate_ecovs(self):
        ex = self.exdf
        o = self.st
        if len(ex)==0:
            return
        ex['ecov'] = N.nan
        for strand in ['+','-']:
            spans = self._get_spans(strand)
            for st,ed in spans:
                idx = (ex['st']>=st)&(ex['ed']<=ed)&(ex['strand'].isin(STRS[strand]))
                es = ex[idx].copy().sort_values(['st','ed'])
                es['tmpeid'] = N.arange(len(es))
                ne = len(es)
                exa = self.arrs['ex'][strand]
                def cov(s,e):
                    return N.mean(exa[s-o:e-o])
                if ne>1:
                    ci = UT.chopintervals(es, idcol='tmpeid', sort=False)
                    ci['cov'] = [cov(s,e) for s,e in ci[['st','ed']].values]
                    ci['name1'] = ci['name'].astype(str).apply(lambda x: [int(y) for y in x.split(',')])    
                    nc = len(ci)
                    mat = N.zeros((nc,ne))
                    for i,n1 in enumerate(ci['name1'].values):# fill in rows
                        N.put(mat[i], N.array(n1), 1)
                    try:
                        ecov,err = nnls(mat, ci['cov'].values)
                        ex.loc[idx,'ecov'] = ecov
                    except:
                        LOG.warning('!!!!!! Exception in NNLS (calculate_ecov) @{0}:{1}-{2}, setting to mean !!!!!!!!!'.format(self.chrom, st, ed))
                        ex.loc[idx,'ecov'] = cov(st,ed)
                elif ne==1:
                    s,e = es.iloc[0][['st','ed']]
                    ex.loc[idx,'ecov'] = cov(s,e)
        # self._ne2ecov = UT.df2dict(ex, 'name', 'ecov')
        idxz = ex['ecov']==0
        ex.loc[idxz, 'ecov'] = 1e-6
                       
    def _get_sub_sjex(self, st, ed, strand):
        sj0 = self.sjdf
        idx = (sj0['strand'].isin(STRS[strand]))&(sj0['st']>=st)&(sj0['ed']<=ed)&(sj0['chr']==self.chrom)
        sj = sj0[idx]
        n0 = len(sj)
        n1 = self.params['pathcheckth'] # 100
        pct = self.params['pathcheckratio'] # 0.2
        if n0>n1:
            n2 = N.sum(sj['ucnt']==0)
            if n2>n0*pct:
                LOG.warning('num sj ({0}>{1}) removed non unique junctions({2})'.format(n0,n1,n2))
                idx5 = sj['ucnt']>0
                sj = sj[idx5]
        sj = sj.copy()
        ex0 = self.exdf
        idx = (ex0['strand'].isin(STRS[strand]))&(ex0['st']>=st)&(ex0['ed']<=ed)&(ex0['chr']==self.chrom)                
        ex = ex0[idx].copy()
        return sj, ex

    def find_groups(self):
        # genes: connected components
        sjs = []
        exs = []
        paths = []
        # self.ggs = ggs = {}
        cnt = 0
        chrom = self.chrom
        for strand in ['+','-']:
            spans = self._get_spans(strand)
            for st,ed in spans:
                spansjs = []
                spanexs = []
                spanpaths = []
                sj,ex = self._get_sub_sjex(st,ed,strand)
                if len(sj)==0:
                    continue
                self._gg = gg = GeneGraph(sj,ex,strand)
                # ggs[cnt] = gg
                self._genes = genes = gg.find_genes()
                for gsj,gex in genes:
                    sjexs5 = gg.find_5groups(gsj,gex)
                    for gsj5,gex5 in sjexs5:
                        sjexs53 = gg.find_53groups(gsj5,gex5)
                        for gsj53,gex53 in sjexs53:
                            self.calc_53branchp(gg, gsj53,gex53)
                            spansjs.append(gsj53)
                            spanexs.append(gex53)
                if len(spansjs)>0 and len(spanexs)>0:
                    self._spansjdf = spansjdf = PD.concat(spansjs, ignore_index=True)
                    self._spanexdf = spanexdf = PD.concat(spanexs, ignore_index=True)
                    self.calc_span_tcov(spansjdf, spanexdf, strand)
                    spanpathdf = self.select_53paths(gg, spansjdf, spanexdf, chrom, strand)
                    sjs.append(spansjdf)
                    exs.append(spanexdf)
                    paths.append(spanpathdf)
                cnt += 1
        sjsdf = PD.concat(sjs, ignore_index=True)
        exsdf = PD.concat(exs, ignore_index=True)
        pathsdf = PD.concat(paths, ignore_index=True)
        self.sjdf2 = sjsdf
        self.exdf2 = exsdf
        self.paths = pathsdf

    def calc_53branchp(self, gg, sj, ex):
        dsump = sj.groupby('dpos')['tcnt'].sum().astype(float)
        sj['p'] = jdp = sj['tcnt'].values/(dsump.ix[sj['dpos'].values].values)
        # j2p = dict(zip(sj['name'].values, jdp))
        # exon groupby acceptor
        asump = ex.groupby('apos')['ecov'].sum().astype(float)
        ex['pa'] = eap = ex['ecov'].values/(asump.ix[ex['apos'].values].values)
        dsump = ex.groupby('dpos')['ecov'].sum().astype(float)
        ex['pd'] = edp = ex['ecov'].values/(dsump.ix[ex['dpos'].values].values)
        if (N.sum(N.isnan(eap))>0):
            self._cblcls = locals()
            raise

    def select_53paths(self, gg, spansjdf, spanexdf, chrom, strand):
        paths = []
        for gid in spanexdf['gid'].unique():
            gexdf = spanexdf[spanexdf['gid']==gid]
            gsjdf = spansjdf[spansjdf['gid']==gid]
            pg = PathGenerator(gg, gsjdf, gexdf, chrom, strand, self.sjpaths, self.params['upperpathnum'])
            paths.append(pg.select_paths(self.params['tcovth']))
        return PD.concat(paths, ignore_index=True)

    def calc_span_tcov(self, spansjs, spanexs, strand):
        o = self.st
        exa = self.arrs['ex'][strand]
        sja = self.arrs['sj'][strand]
        # ne2ecov = self._ne2ecov
        def cov0(s,e):
            return N.mean(sja[s-o:e-o])
        def cov1s(s):
            s0 = max(0, s-o-10)
            s1 = max(s0+1,s-o)
            return N.mean(exa[s0:s1])
        def cov1e(e):
            return N.mean(exa[e-o:e-o+10])
        def cov2s(s):
            s0 = max(0, s-o-1)
            return sja[s-o]-sja[s0]
        def cov2e(e):
            e0 = max(0, e-o-1)
            return sja[e-o]-sja[e0]     

        pg = spanexs.groupby('id53').first().sort_values(['tst','ted'])[['chr','tst','ted']]
        ne = len(pg)
        if ne>1:
            pg.rename(columns={'tst':'st','ted':'ed'}, inplace=True)
            pg['eid'] = N.arange(len(pg))
            ci = UT.chopintervals(pg, idcol='eid')
            ci['cov'] = [cov0(s,e) for s,e in ci[['st','ed']].values]
            ci['name1'] = ci['name'].astype(str).apply(lambda x: [int(y) for y in x.split(',')])    
            nc = len(ci)
            mat = N.zeros((nc,ne))
            for i,n1 in enumerate(ci['name1'].values):# fill in rows
                N.put(mat[i], N.array(n1), 1)
            try:
                ecov,err = nnls(mat, ci['cov'].values)
                pg['tcov0a'] = ecov
            except:
                # too much iteration?
                LOG.warning('!!!!!! Exception in NNLS (tcov_by_nnls) @{0}:{1}-{2}, setting to zero !!!!!!!!!'.format(self.chrom, s, e))
                pg['tcov0a'] = 0
            pg.rename(columns={'st':'tst','ed':'ted'}, inplace=True)
        else:
            s,e = pg.iloc[0][['tst','ted']]
            pg['tcov0a'] = cov0(s,e)

        if ne>1:
            sts = sorted(set(pg['tst'].values))
            eds = sorted(set(pg['ted'].values))
            nst,ned = len(sts),len(eds)
            mat = N.array([(pg['tst']==x).values for x in sts]+[(pg['ted']==x).values for x in eds], dtype=float)
            c = N.array([cov1s(x) for x in sts]+[cov1e(x) for x in eds])
            # enforce flux conservation: scale up 5'
            stsum = N.sum(c[:nst])
            edsum = N.sum(c[nst:])
            if strand in ['+','.+']:
                c[:nst] = (edsum/(stsum+1e-6))*c[:nst]
            else:
                c[nst:] = (stsum/(edsum+1e-6))*c[nst:]
            ecov,err = nnls(mat, c)
            pg['tcov0b'] = ecov

            mat = N.array([(pg['tst']==x).values for x in sts]+[-1*(pg['ted']==x).values for x in eds], dtype=float)
            c = N.array([cov2s(x) for x in sts]+[cov2e(x) for x in eds])
            # enforce flux conservation: scale up 5'
            stsum = N.sum(c[:nst])
            edsum = N.sum(c[nst:])
            if strand in ['+','.+']:
                c[:nst] = ((-1*edsum)/(stsum+1e-6))*c[:nst]
            else:
                c[nst:] = ((-1*stsum)/(edsum+1e-6))*c[nst:]
            ecov,err = nnls(mat, c)
            pg['tcov0c'] = ecov
        else:
            s,e = pg.iloc[0][['tst','ted']]
            pg['tcov0b'] = (cov1s(s)+cov1e(e))/2.
            pg['tcov0c'] = (cov2s(s)-cov2e(e))/2.

        pg['tcov0'] = pg[['tcov0a','tcov0b','tcov0c']].mean(axis=1)
        pg.loc[pg['tcov0']<0,'tcov0'] = 0 # shouldn't really happen

        exkeys = spanexs['id53'].values
        sjkeys = spansjs['id53'].values
        for f in ['tcov0','tcov0a','tcov0b','tcov0c']:
            spanexs[f] = pg.ix[exkeys][f].values
            spansjs[f] = pg.ix[sjkeys][f].values     

    def write(self, cmax=9):
        pre = self.dstpre+'.{0}_{1}_{2}'.format(self.chrom,self.st,self.ed)
        # 1) exon, junctions, allpaths => csv (no header <= to concatenate bundles)
        
        ecols = EXDFCOLS #['chr','st','ed','strand','name','kind','ecov']
        UT.write_pandas(self.exdf[ecols], pre+'.exdf.txt.gz', '')
        
        scols = SJDFCOLS #['chr','st','ed','strand','name','kind','tcnt','ucnt']
        UT.write_pandas(self.sjdf[scols], pre+'.sjdf.txt.gz', '')
        
        pcols = PATHCOLS #['chr','st','ed','name','strand','tst','ted','tcov', 'tcov0','tcov0a,b,c']
        if len(self.paths)>0:
            UT.write_pandas(self.paths[pcols], pre+'.paths.txt.gz', '')
            self.bed12 = path2bed12(self.paths.copy(), cmax, 'tcov')
            GGB.write_bed(self.bed12, pre+'.paths.bed.gz',ncols=12)
            self.tspan = path2tspan(self.paths.copy(), cmax, 'tcov0')
            GGB.write_bed(self.tspan, pre+'.tspans.bed.gz',ncols=12)

        # 2) unused sjpaths => bed12
        sjpaths0 = self._sjpaths0
        allpathcode = '$'.join(self.paths['name'].values)
        idxused = N.array([y in allpathcode for y in sjpaths0['name']])
        self.unusedsj = sjpaths0[~idxused]         
        self.usedsj = sjpaths0[idxused]
        GGB.write_bed(self.unusedsj, pre+'.unused.sjpath.bed.gz', ncols=12)

    def draw_covs(self, st, ed, strand, win=500, ax=None):
        if ax is None:
            fig,ax = P.subplots(1,1,figsize=(15,3))
        offset = self.st
        s0 = st-win-offset
        e0 = ed+win-offset
        sjap0 = self.arrs['sj'][strand][s0:e0]
        exap0 = self.arrs['ex'][strand][s0:e0]
        sjap1 = self.filled[strand][s0:e0]
        y0 = N.log2(sjap1+1)
        ax.plot(y0, 'r-', alpha=0.8)
        #ax.plot(N.log2(sjap0+1), 'r--')
        ipx = set(N.nonzero(exap0>0)[0])
        n = len(exap0)
        ipx.update([x+1 for x in ipx if x<n-1])
        ipx.update([x-1 for x in ipx if x>1])
        ipx = sorted(ipx)
        ax.fill_between(ipx, 0, N.log2(exap0[ipx]+1), facecolor='m', alpha=0.3)
        # gspan
        gspan = self._get_spans(strand)
        h0 = 15
        for i, (s1,e1) in enumerate(gspan):
            if (e1-offset>s0)&(s1-offset<e0):
                # print('gspan {0}:{1}-{2}'.format(i,s1,e1))
                gx1,gx2 = s1-s0-offset,e1-s0-offset
                ax.plot([gx1,gx2],[h0+2,h0+2], 'c')
                gx0 = max(min((gx1+gx2)/2., e0-s0), 0)
                ax.text(gx0, h0-2, '{0}'.format(i))
        # 53
        e53p = self.e53pos[strand]
        t5 = e53p[(e53p['kind']=='5')&(e53p['pos']>s0)&(e53p['pos']<e0)]
        t3 = e53p[(e53p['kind']=='3')&(e53p['pos']>s0)&(e53p['pos']<e0)]
        i5p = N.array(t5['pos'].values-s0, dtype=N.int64)
        i3p = N.array(t3['pos'].values-s0, dtype=N.int64)
        if len(i5p)>0:
            ax.plot(i5p, y0[i5p], 'm^')
        if len(i3p)>0:
            ax.plot(i3p, y0[i3p], 'mv')

        # exons
        ex = self.exons[strand]
        ex = ex[(ex['ost']>s0)&(ex['oed']<e0)]
        ymid = h0+5
        h = 2
        yrange = (ymid-h/2., h)
        xranges = [(x-s0,y-x) for x,y in ex[['ost','oed']].values]
        cargs = dict(facecolor='k', edgecolor='k')#, linewidth=0.2)
        bbhc = BrokenBarHCollection(xranges, yrange, **cargs)
        ax.add_collection(bbhc)

        # exdfi, e53df
        def _plt_ex(ex, ymid, c):
            ex = ex[(ex['st']<ed)&(ex['ed']>st)&(ex['strand'].isin(STRS[strand]))]
            h = 2
            yrange = (ymid-h/2., h)
            xranges = [(x-s0-offset,y-x) for x,y in ex[['st','ed']].values]
            cargs = dict(facecolor=c, edgecolor=c)#, linewidth=0.2)
            bbhc = BrokenBarHCollection(xranges, yrange, **cargs)
            ax.add_collection(bbhc)
        if hasattr(self, 'exdfi'):
            _plt_ex(self.exdfi, h0+2, 'g')
        if hasattr(self, 'e53fixed'):
            _plt_ex(self.e53fixed[self.e53fixed['kind']=='5'], h0+2, 'r')
            _plt_ex(self.e53fixed[self.e53fixed['kind']=='3'], h0+2, 'b')
        if hasattr(self, 'e53df'):
            e = self.e53df
            _plt_ex(e[(e['kind']=='5')&(e['origin']=='path')], h0, 'r')
            _plt_ex(e[(e['kind']=='3')&(e['origin']=='path')], h0, 'b')
            _plt_ex(e[(e['kind']=='5')&(e['origin']=='flow')], h0, 'm')
            _plt_ex(e[(e['kind']=='3')&(e['origin']=='flow')], h0, 'c')

        ax.set_xlim(0,e0-s0)
        ax.set_ylim(-2,h0+12)
        ax.set_yticks([0,5,9])
        ax.set_xticks([])
        txt = '{0}:{1}-{2}:{3}'.format(self.chrom, st-win, ed+win, strand)
        print(txt)
        ax.text(0,h0+7, txt)
        ax.set_frame_on(False)        
        return ax
        
    def draw_path(self, pathdf, st, ed, strand, covfld='tcov',win=500, ax=None, delta=500):
        if ax is None:
            fig,ax = P.subplots(1,1,figsize=(15,3))
        st0 = st-win
        ed0 = ed+win
        idx = (((pathdf['tst']>=st0)&(pathdf['tst']<=ed0))|\
              ((pathdf['ted']>=st0)&(pathdf['ted']<=ed0)))&\
              (pathdf['strand'].isin(STRS[strand]))&(pathdf['chr']==self.chrom)
        df = pathdf[idx].sort_values(['tst','ted']).copy()
        esiz = 100
        h = 2
        cnt = 0
        cted = 0
        minypos = 0
        lss = {'+':'-','-':'-','.+':'--','.-':'--','.':'--'}
        cbs = Colors('gray_r',1.,0.)
        cls = {'+':Colors('R',1.,0.),'-':Colors('B',1.,0.),
               '.+':Colors('gray_r',1.,0.),'.-':Colors('gray_r',1.,0.),
               '.':Colors('gray_r',1.,0.)}
        if covfld not in df.columns:
            df[covfld] = 1.
        df['ltcov'] = N.log2(df[covfld]+2)
        df['tcovn'] = df['ltcov']/df['ltcov'].max()
        for pc, tst, ted, s, tcov in df[['name','tst','ted','strand','tcovn']].values:
            if cted+delta>tst:
                cnt +=1
            else:
                cnt = 0
            cted = max(ted, cted)
            ymid = -cnt*(h+1)
            minypos = min(ymid, minypos)
            cb = cbs.to_rgba(tcov)
            cl = cls[s].to_rgba(tcov)
            ls = lss[s]
            cargs = dict(facecolor=cb, edgecolor=cb)
            ax.plot([tst-st0,ted-st0],[ymid,ymid],ls=ls, color=cl)
            yrange = (ymid-h/2., h)
            tmp = pc.split(',')
            try:# without 5',3' exons
                # pathcode = dpos0|apos1,dpos1|apos2,...dpos(n-1)|aposn
                tst = int(tmp[0].split('|')[0])
                ted = int(tmp[-1].split('|')[1])
                if tst<ted:
                    tmppc = str(tst-esiz)+','+pc+','+str(ted+esiz)
                else:
                    tmppc = str(tst+esiz)+','+pc+','+str(ted-esiz)                
            except:# with 5',3' exons
                # pathcode = 5pos,dpos0|apos1,dpos1|apos2,...dpos(n-1)|aposn,3pos
                tst = int(tmp[1].split('|')[0])
                ted = int(tmp[-2].split('|')[1])
                tmppc = pc
            if tst<ted:
                exons = [[int(x) for x in y.split(',')] for y in tmppc.split('|')]
            else:
                exons = [[int(x) for x in y.split(',')] for y in tmppc.split('|')]
                exons = [x[::-1] for x in exons[::-1]]
            xranges = [(x-st0,y-x) for x,y in exons]
            bbhc = BrokenBarHCollection(xranges, yrange, **cargs)
            ax.add_collection(bbhc)
        ax.set_ylim(minypos-5, 5)
        ax.set_xlim(0,ed0-st0)
        ax.set_yticks([])
        ax.set_xticks([])
        ax.set_frame_on(False)
        return ax

    def drawspan2(self,st,ed,strand,df2, win=500, figsize=(15,6),  delta=500, df2cov='sc2', maxdisp=None):
        fig, axr = P.subplots(2,1,figsize=figsize,sharex=True)
        P.subplots_adjust(hspace=0)
        self.draw_covs(st,ed,strand, ax=axr[0], win=win)
        if maxdisp is not None:
            df2 = df2.sort_values(df2cov,ascending=False).iloc[:maxdisp]
        self.draw_path(df2, st,ed,strand, ax=axr[1], covfld=df2cov, win=win, delta=delta)

    def drawspan2pn(self,st,ed,df2, win=500, figsize=(15,6),  delta=500, df2cov='sc2', maxdisp=None):
        fig, axr = P.subplots(4,1,figsize=figsize,sharex=True)
        P.subplots_adjust(hspace=0)

        if maxdisp is not None:
            df2 = df2.sort_values(df2cov,ascending=False).iloc[:maxdisp]

        self.draw_covs(st,ed,'+', ax=axr[0], win=win)
        self.draw_path(df2, st,ed,'+', ax=axr[1], covfld=df2cov, win=win, delta=delta)

        self.draw_covs(st,ed,'-', ax=axr[2], win=win)
        self.draw_path(df2, st,ed,'-', ax=axr[3], covfld=df2cov, win=win, delta=delta)

    def drawspan3(self,st,ed,strand,df2,df3,win=500, figsize=(15,9), delta=500, 
        df2cov='sc2', df3cov='tcov', maxdisp=None):
        fig, axr = P.subplots(3,1,figsize=figsize,sharex=True)
        P.subplots_adjust(hspace=0)
        self.draw_covs(st,ed,strand, ax=axr[0], win=win)
        if maxdisp is not None:
            df2 = df2.sort_values(df2cov,ascending=False).iloc[:maxdisp]
        self.draw_path(df2, st,ed,strand, ax=axr[1], covfld=df2cov, win=win, delta=delta)
        if maxdisp is not None:
            df3 = df3.sort_values(df2cov,ascending=False).iloc[:maxdisp]
        self.draw_path(df3, st,ed,strand, ax=axr[2], win=win, delta=delta, covfld=df3cov)

    def drawspan3pn(la,st,ed,win=10000, figsize=(15,6), df2=None, df3=None, delta=500,
        df2cov='sc2', df3cov='tcov', maxdisp=None):
        o = la.st
        fig, axr = P.subplots(6,1,figsize=figsize,sharex=True)
        P.subplots_adjust(hspace=0)
        strand = '+'
        la.draw_covs(st,ed,strand, ax=axr[0], win=win)
        if df2 is not None:
            if maxdisp is not None:
                df2 = df2.sort_values(df2cov,ascending=False).iloc[:maxdisp]
            la.draw_path(df2, st,ed,strand, ax=axr[1], covfld=df2cov, win=win, delta=delta)
        if df3 is not None:
            if maxdisp is not None:
                df3 = df3.sort_values(df2cov,ascending=False).iloc[:maxdisp]
            la.draw_path(df3, st,ed,strand, ax=axr[2], win=win, delta=delta, covfld=df3cov)
        strand = '-'
        la.draw_covs(st,ed,strand, ax=axr[3], win=win)
        if df2 is not None:
            if maxdisp is not None:
                df2 = df2.sort_values(df2cov,ascending=False).iloc[:maxdisp]
            la.draw_path(df2, st,ed,strand, ax=axr[4], covfld=df2cov, win=win, delta=delta)
        if df3 is not None:
            if maxdisp is not None:
                df3 = df3.sort_values(df2cov,ascending=False).iloc[:maxdisp]
            la.draw_path(df3, st,ed,strand, ax=axr[5], win=win, delta=delta, covfld=df3cov)

def draw_sjex(sj, ex, st, ed, win=500, ax=None, delta=500, sjcov='tcnt', excov='ecov'):
    if ax is None:
        fig,ax = P.subplots(1,1,figsize=(15,3))
    sj = sj.copy()
    ex = ex.copy()
    st0 = st-win
    ed0 = ed+win
    h = 2
    esize=100
    cnt = 0
    cted = 0
    minypos = 0
    lss = {'+':'-','-':'-','.+':'--','.-':'--'}
    cbs = Colors('gray_r',1.,0.)
    cls = {'+':Colors('R',1.,0.),'-':Colors('B',1.,0.),
           '.+':Colors('gray_r',1.,0.),'.-':Colors('gray_r',1.,0.)}
    if sjcov not in sj:
        sj[sjcov] = 1.
    if excov not in ex:
        ex[excov] = 1.
    sj['ltcov'] = N.log2(sj[sjcov]+2)
    sj['tcovn'] = sj['ltcov']/sj['ltcov'].max()
    ex['ltcov'] = N.log2(ex[excov]+2)
    ex['tcovn'] = ex['ltcov']/ex['ltcov'].max()

    # sj
    for pc, tst, ted, s, tcov in sj[['name','st','ed','strand','tcovn']].values:
        if cted+delta>tst:
            cnt +=1
        else:
            cnt = 0
        cted = max(ted, cted)
        ymid = -cnt*(h+1)
        minypos = min(ymid, minypos)
        cb = cbs.to_rgba(tcov)
        cl = cls[s].to_rgba(tcov)
        ls = lss[s]
        cargs = dict(facecolor=cb, edgecolor=cb)
        ax.plot([tst-st0,ted-st0],[ymid,ymid],ls=ls, color=cl,marker='o')
    # ex
    yrange = (4-h/2., h)
    exons = ex.sort_values(['st','ed'])[['st','ed']].values
    xranges = [(x-st0,y-x) for x,y in exons]
    cargs = dict(facecolor='k', edgecolor='k')
    bbhc = BrokenBarHCollection(xranges, yrange, **cargs)
    ax.add_collection(bbhc)

    ax.set_ylim(minypos-5, 5)
    ax.set_xlim(0,ed0-st0)
    ax.set_yticks([])
    ax.set_xticks([])
    ax.set_frame_on(False)
    return ax


class PathGenerator(object):
    # gene level path generator

    def __init__(self, gg, gsjdf, gexdf, chrom, strand, sjpaths, upperpathnum=100):
        self.gg = gg # GeneGraph
        self.gexdf = gexdf # gene exons
        self.gsjdf = gsjdf # gene junctions
        self.st = st = gexdf['st'].min()
        self.ed = ed = gexdf['ed'].max()
        self.chrom = chrom 
        self.strand = strand
        idx = (sjpaths['tst']>=st)&(sjpaths['ted']<=ed)&(sjpaths['strand'].isin(STRS[strand]))&(sjpaths['chr']==chrom)
        self.sjpaths = sjpaths[idx]
        self.e5s = e5s = gexdf[gexdf['kind']=='5']
        self.upperpathnum = upperpathnum
        self.pg53s = [PathGenerator53(x,gg,gexdf,gsjdf,upperpathnum) for i,x in e5s.iterrows()] # one unit

    def paths_from_highest_cov(self):
        if len(self.gexdf)==0:
            return
        vmax = self.gexdf['tcov0'].max()
        delta = vmax/5.
        vmin = vmax - delta
        while vmax>0:
            try:
                l = [x.get_paths_range(vmin,vmax) for x in self.pg53s]
                l = [x for x in l if x is not None]
                if len(l)>0:
                    paths = PD.concat(l, ignore_index=True)
                    paths.sort_values('tcov', ascending=False)
                    # print(paths)
                    for rec in paths.values:
                        yield rec
                else:
                    delta = 1.5*delta
                # print(vmax,vmin, len(l))
                vmax = vmin
                vmin = max(0, vmax - delta)
            except PathNumUpperLimit:
                vmin0 = vmin
                vmin = (vmax+vmin)/2.
                print('PathNumUpperLimit, increase vmin {0}=>{1}'.format(vmin0, vmin))

    def select_paths(self, tcovth):
        sjp = self.sjpaths
        nsj = len(sjp)
        # print('nsj', nsj)
        z = N.zeros(nsj)
        # sjnames = [','.join(x.split(',')[1:-1]) for x in sjp['name'].values]
        sjnames = sjp['name'].values
        # print(sjnames)
        npos = PATHCOLS.index('name')
        tpos = PATHCOLS.index('tcov')
        paths = []
        cscore = 0
        th = self.upperpathnum*10
        for p in self.paths_from_highest_cov():
            # print('*** check one ***')
            if p[tpos]>tcovth:
                paths.append(p)
                for i,sjn in enumerate(sjnames):
                    if z[i]==0:
                        z[i] += (sjn in p[npos])
                cscore = N.sum(z>0)
                # if cscore==nsj:
                #     print('all covered break 1', cscore, nsj, z)
                #     break
            else:
                if cscore==nsj: # all sjpaths covered
                    # print('all covered break 2', cscore, nsj, z)
                    break
                else:
                    for i,sjn in enumerate(sjnames):
                        if z[i]==0:
                            z[i] += (sjn in p[npos])
                    cscore1 = N.sum(z>0)
                    if cscore1>cscore:
                        paths.append(p)
                        cscore = cscore1
            # print('cscore', cscore, nsj, z)
            if len(paths)>th:
                tcov = paths[-1][tpos]
                txt = '#path (0}>{1} terminate path enumeration. score:{2}/{3} tcov:{4}'
                LOG.warning(txt.format(len(paths),th,cscore,nsj,tcov))
                break
        return PD.DataFrame(paths, columns=PATHCOLS)

class PathGenerator53(object):

    def __init__(self, e5, gg, gexdf, gsjdf, upperpathnum=100):
        # edges
        self.e5 = e5
        self.gg = gg
        self.id53 = id53 = e5['id53']
        self.exdf = gexdf[gexdf['id53']==id53]
        self.sjdf = gsjdf[gsjdf['id53']==id53]
        self.sids = sids = gsjdf[gsjdf['id53']==id53]['sid'].values
        self.eids = eids = gexdf[gexdf['id53']==id53]['eid'].values
        self.edj = edj = {e: [x for x in gg.edj.get(e,[]) if x in sids] for e in eids}
        self.jae = jae = {j: [x for x in gg.jae.get(j,[]) if x in eids] for j in sids}
        self.j2p = j2p = UT.df2dict(self.sjdf, 'sid', 'p')
        self.e2p = e2p = UT.df2dict(self.exdf, 'eid', 'pa')
        self.ede = ede = {e: [x for x in gg.ede[e] if x in eids] for e in eids if e in gg.ede}
        self.tcov = e5['tcov0']*e5['pd']
        self.e2mima = {}
        self.e2ep = {e: [(y, e2p[y]*j2p[x]) for x in edj[e] for y in jae[x]] for e in eids}
        self.e2kind = UT.df2dict(self.exdf, 'eid', 'kind')
        self.e2name = UT.df2dict(self.exdf, 'eid', 'name')
        self.upperpathnum = upperpathnum

        
    def get_paths_range(self, vmin, vmax):
        if self.tcov <= vmin:
            return None
        # get paths whose tcov is >vmin & <=vmax
        # do BFS, ignore branch whose tmax<=vmin or tmin>vmax
        # (chr,st,ed,strand,name(pc),tcov0,tcov1,e5dp,tcov,tst,ted,tcov0a,tcov0b,tcov0c)
        recs, pmax, pmin = self.dfs(self.e5['eid'], vmin, vmax, '', self.tcov)
        if len(recs)==0:
            return None
        pathsdf = PD.DataFrame(recs, columns=['name','st','ed','tcov'])
        # paths: st,ed,name,tcov
        for f in ['chr','strand','tst','ted','tcov0','tcov0a','tcov0b','tcov0c']:
            pathsdf[f] = self.e5[f]
        return pathsdf[PATHCOLS]

    def dfs(self, eid, vmin, vmax, pathpre, ctcov):
        if (self.e2kind[eid]=='3') or (len(self.e2ep.get(eid,[]))==0): # leaf
            # print('   leaf', eid)
            if (ctcov>vmax) or (ctcov<=vmin):
                # print('   out of range')
                return ([], 1, 1)
            pathname = pathpre+'|'+self.e2name[eid] if pathpre else self.e2name[eid]
            tmp = pathname.split(',')
            st = int(tmp[0])
            ed = int(tmp[-1])
            if st>ed:
                st,ed = ed,st
            self.e2mima[eid] = (1,1)
            return ([[pathname,st,ed,ctcov]],1, 1)
        # non-leaf recurse
        recs = []
        pmins = []
        pmaxs = []
        pathpre0 = pathpre + '|' + self.e2name[eid] if pathpre else self.e2name[eid]
        if len(self.e2ep[eid])==0:
            print('wrong e2ep', eid, self.e2ep, self.e2kind, self.exdf, self.sjdf)
            raise
        for e, p in self.e2ep[eid]:
            ctcov0 = ctcov*p
            if e in self.e2mima:
                pmin,pmax= self.e2mima[e]
                if (ctcov0*pmax>vmin)&(ctcov0*pmin<=vmax):
                    recs1, pmax, pmin = self.dfs(e, vmin, vmax, pathpre0, ctcov0)
                    recs += recs1
                pmins.append(pmin*p)
                pmaxs.append(pmax*p)
            else:
                if ctcov0 > vmin:
                    # print('-----recurse', eid, vmin, vmax, ctcov0)
                    recs1, pmax, pmin = self.dfs(e, vmin, vmax, pathpre0, ctcov0)
                    recs += recs1
                    # print('-----recs:',eid, len(recs))
                    pmins.append(pmin*p)
                    pmaxs.append(pmax*p)
                else:
                    pmins.append(0)
                    pmaxs.append(p)
            if len(recs)>self.upperpathnum:
                raise PathNumUpperLimit

        pmin0 = N.min(pmins)
        pmax0 = N.max(pmaxs)
        self.e2mima[eid] = (pmin0, pmax0)
        return recs, pmax0, pmin0

class PathNumUpperLimit(Exception):
    pass


####### Writers  #####################################################################

def path2bed12(paths, cmax=9, covfld='tcov'):
    bed = paths
    # strand .+,.-
    idxun = bed['strand']=='.-'
    bed.loc[idxun, 'name']= [reversepathcode(x) for x in bed[idxun]['name']]
    bed = bed.groupby('name').first().reset_index()
    idxu = bed['strand'].isin(['.+','.-'])
    bed.loc[idxu, 'strand']='.'
    # #exons, esizes, estarts
    bed['exonsp'] = [[[int(z) for z in y.split(',')] for y in x.split('|')] for x in bed['name']]
    bed['exonsn'] = [[y[::-1] for y in x][::-1] for x in bed['exonsp']]
    idxp = bed['strand']!='-'
    bed.loc[idxp, 'exons'] = bed[idxp]['exonsp']
    bed.loc[~idxp, 'exons'] = bed[~idxp]['exonsn']
    bed['#exons'] = [len(x) for x in bed['exons']]
    estarts = [[str(y[0]-x[0][0]) for y in x] for x in bed['exons']]
    esizes = [[str(y[1]-y[0]) for y in x] for x in bed['exons']]
    bed['esizes'] = [','.join(x)+',' for x in esizes]
    bed['estarts'] = [','.join(x)+',' for x in estarts]
    # sc1, sc2
    bed['ltcov'] = N.log2(bed[covfld]+2)
    # bed['sc1'] = N.ceil(bed['ltcov']*100).astype(int)
    bed['sc1'] = bed[covfld]
    sm = {'+':Colors('R', cmax),
          '-':Colors('B', cmax),
          '.':Colors('G', cmax)}
    bed['sc2'] = [sm[s].RGB(x) for x,s in bed[['ltcov','strand']].values]
    bed.sort_values(['chr','st','ed'], inplace=True)
    return bed

def path2tspan(paths, cmax=9, covfld='tcov0'):
    bed = paths
    # strand .+,.-
    idxun = bed['strand']=='.-'
    bed.loc[idxun, 'name']= [reversepathcode(x) for x in bed[idxun]['name']]
    bed = bed.groupby('name').first().reset_index()
    idxu = bed['strand'].isin(['.+','.-'])
    bed.loc[idxu, 'strand']='.'
    # #exons, esizes, estarts
    bed['exonsp'] = [[[int(z) for z in y.split(',')] for y in x.split('|')] for x in bed['name']]
    bed['exonsn'] = [[y[::-1] for y in x][::-1] for x in bed['exonsp']]
    idxp = bed['strand']!='-'
    bed.loc[idxp, 'exons'] = bed[idxp]['exonsp']
    bed.loc[~idxp, 'exons'] = bed[~idxp]['exonsn']

    # group same (tst,ted) ##############
    bg = bed.groupby(['tst','ted'])
    bedg = bg.first()
    bedg['exons'] = bg['exons'].apply(lambda g: sorted(set([tuple(x) for y in g for x in y])))
    bedg['st'] = bg['st'].min()
    bedg['ed'] = bg['ed'].max()
    bed = bedg.reset_index()
    ######################################

    bed['#exons'] = [len(x) for x in bed['exons']]
    estarts = [[str(y[0]-x[0][0]) for y in x] for x in bed['exons']]
    esizes = [[str(y[1]-y[0]) for y in x] for x in bed['exons']]
    bed['esizes'] = [','.join(x)+',' for x in esizes]
    bed['estarts'] = [','.join(x)+',' for x in estarts]
    # sc1, sc2
    bed['ltcov'] = N.log2(bed[covfld]+2)
    # bed['sc1'] = N.ceil(bed['ltcov']*100).astype(int)
    bed['sc1'] = bed[covfld]
    sm = {'+':Colors('R', cmax),
          '-':Colors('B', cmax),
          '.':Colors('G', cmax)}
    bed['sc2'] = [sm[s].RGB(x) for x,s in bed[['ltcov','strand']].values]
    bed.sort_values(['chr','st','ed'], inplace=True)
    return bed    

def sjpaths2tspan(sjpaths, cmax=9, strip53=False, sc2color=True):
    # bed12
    bed = sjpaths
    # #exons, esizes, estarts
    bed['exonsp'] = [[[int(z) for z in y.split(',')] for y in x.split('|')] for x in bed['name']]
    bed['exonsn'] = [[y[::-1] for y in x][::-1] for x in bed['exonsp']]
    idxp = bed['strand']!='-'
    bed.loc[idxp, 'exons'] = bed[idxp]['exonsp']
    bed.loc[~idxp, 'exons'] = bed[~idxp]['exonsn']

    bg = sjpaths.groupby(['tst','ted'])
    bedg = bg.first()
    bedg['exons'] = bg['exons'].apply(lambda g: sorted(set([tuple(x) for y in g for x in y])))
    bedg['st'] = bg['st'].min()
    bedg['ed'] = bg['ed'].max()
    bedg['sc1'] = bg['sc1'].sum()
    bed = bedg.reset_index()
    
    bed['#exons'] = [len(x) for x in bed['exons']]
    estarts = [[str(y[0]-x[0][0]) for y in x] for x in bed['exons']]
    esizes = [[str(y[1]-y[0]) for y in x] for x in bed['exons']]
    bed['esizes'] = [','.join(x)+',' for x in esizes]
    bed['estarts'] = [','.join(x)+',' for x in estarts]
    if sc2color:
        bed['ltcov'] = N.log2(bed['sc1']+2)
        sm = {'+':Colors('R', cmax),
              '-':Colors('B', cmax),
              '.':Colors('G', cmax)}
        bed['sc2'] = [sm[s].RGB(x) for x,s in bed[['ltcov','strand']].values]
    else:
        bed['sc2'] = bed['sc1']
    if strip53:
        bed['name'] = [','.join(x.split(',')[1:-1]) for x in bed['name']]
    bed.sort_values(['chr','st','ed'], inplace=True)
    return bed

####### Gene Graph ###################################################################

class GeneGraph(object):

    def __init__(self, sjs, exs, strand, depth=500):
        self.sjs = sjs #= sjs[sjs['strand'].isin(STRS[strand])].copy()
        self.exs = exs #= exs[exs['strand'].isin(STRS[strand])].copy()
        self.strand = strand
        self.depth = depth

        if '+' in strand:
            exs.sort_values(['st','ed'], inplace=True)
            sjs.sort_values(['st','ed'], inplace=True)
        else:
            exs.sort_values(['ed','st'], inplace=True, ascending=False)
            sjs.sort_values(['ed','st'], inplace=True, ascending=False)
        exs['eid'] = N.arange(1,len(exs)+1)
        sjs['sid'] = N.arange(1,len(sjs)+1)

        if strand=='+':
            sjs['apos'] = sjs['ed']
            sjs['dpos'] = sjs['st']
            exs['apos'] = exs['st']
            exs['dpos'] = exs['ed']
        else:
            sjs['apos'] = sjs['st']
            sjs['dpos'] = sjs['ed']
            exs['apos'] = exs['ed']
            exs['dpos'] = exs['st']
        etbl = exs[['apos','eid','dpos','kind']]
        stbl = sjs[['apos','sid','dpos']]
        etbl1 = etbl.rename(columns={'apos':'apos1','eid':'eid1'})
        etbl2 = etbl.rename(columns={'eid':'eid2','dpos':'dpos2'})
        etbl1 = etbl1[etbl1['kind']!='3']
        etbl2 = etbl2[etbl2['kind']!='5']
        j1 = PD.merge(etbl1, stbl, how='outer', on='dpos', sort=False)
        j2 = PD.merge(j1, etbl2, how='outer', on='apos', sort=False)
        self.j2 = j2
        def _dic(f1,f2):
            t = j2.groupby(f1)[f2].apply(lambda x: [int(y) for y in set(x) if not N.isnan(y)])
            return dict(zip(t.index.values, t.values))
        try:
            # exon|donor => junc => acceptor|exon
            self.ede = ede = _dic('eid1','eid2')
            # exon|acceptor <= junc <= donor|exon
            self.eae = eae = _dic('eid2','eid1')
            self.e2e = {e:set(ede.get(e,[])+eae.get(e,[])) for e in exs['eid']}
            # junc => acceptor|exon                       
            self.jae = jae = _dic('sid','eid2')
            # junc <= donor|exon
            self.jde = jde = _dic('sid','eid1')
            # exon|donor => junc
            self.edj = edj = _dic('eid1','sid')
            # exon|acceptor => junc
            self.eaj = eaj = _dic('eid2','sid')
        except:
            print(exs)
            print(sjs)
            print(j2)
            print(j2.groupby('eid1')['eid2'].apply(lambda x: [int(y) for y in set(x) if not N.isnan(y)]))
            raise
    def connected_nr(self, eid):
        to_visit = [eid]
        exx = set()
        depth=0
        flag = False
        e2e = self.e2e                  
        while(len(to_visit)>0):
            depth+=1
            c = to_visit.pop(0)
            if depth>self.depth:
                flag = True
            exx.add(c)
            for e in e2e[c]:
                if (e not in exx) and (e not in to_visit):
                    to_visit.append(e)
        if flag:
            LOG.debug('eid={1} last visit = {2}: depth {0}'.format(depth,eid,c))         
        return exx

    def allcomponents_nr(self): 
        # ~44sec (sid1624) old version
        exs = self.exs
        self.visited =visited = set()
        self.genes = genes = []
        tot = len(exs)
        for i,eid in enumerate(exs['eid'].values):
            if eid not in visited:
                exx = self.connected_nr(eid)
                genes.append(exx)
                visited.update(exx)
        return genes 

    def find_genes(self):
        gene_eids = self.allcomponents_nr() # [set(eids), ...]
        # find gene_sids
        gene_sids = []
        edj = self.edj
        eaj = self.eaj
        for eids in gene_eids:
            sids = [x for y in eids for x in edj.get(y,[])]
            sids += [x for y in eids for x in eaj.get(y,[])]
            gene_sids.append(list(set(sids)))
        exs = self.exs
        sjs = self.sjs
        genes = []
        for eids, sids in zip(gene_eids, gene_sids):
            dfe = exs[exs['eid'].isin(eids)].copy()
            dfj = sjs[sjs['sid'].isin(sids)].copy()
            chrom = dfe.iloc[0]['chr']
            st = dfe['st'].min()
            ed = dfe['ed'].max()
            gid = '{0}:{1}-{2}:{3}'.format(chrom,st,ed,self.strand)
            if (len(dfe)==0) or (len(dfj)==0):
                # LOG.warning('discarding: len(dfe):{0},len(dfj):{1}, eids:{2}, sids:{3}'.format(len(dfe),len(dfj),eids,sids))
                # print(dfe)
                continue
            dfe['gid'] = gid
            dfj['gid'] = gid
            strand = Counter(dfj['strand'].values).most_common()[0][0]
            dfj['strand'] = strand
            dfe['strand'] = strand
            genes.append((dfj, dfe))
        return genes


    def dfs(self, eid, callback):
        ede = self.ede
        if callback(eid):
            for e in ede.get(eid,[]):
                self.dfs(e, callback)

    def dfs_nr(self, eid, callback):
        ede = self.ede
        to_visit = [eid]
        while(len(to_visit)>0):
            e = to_visit.pop(-1)
            if callback(e):
                to_visit += ede.get(e,[])

    def bfs(self, eid, callback):
        ede = self.ede
        to_visit = [eid]
        while(len(to_visit)>0):
            e = to_visit.pop(0)
            if callback(e):
                to_visit += ede.get(e,[])  

    def get_tree(self, e5id):
        # follow ede get exons and junctions
        visited = set()
        exons = set()
        juncs = set()
        def cb(eid):
            if eid in visited:
                return False
            exons.add(eid)
            juncs.update(self.edj.get(eid,[]))
            visited.add(eid)
            return True
        self.dfs_nr(e5id, cb)
        return [exons, juncs]

    def find_5groups(self, gsjs, gexs):
        g5s = []
        ex5s = gexs[gexs['kind']=='5']
        for e5id in ex5s['eid']:
            eids, sids = self.get_tree(e5id)
            dfe = gexs[gexs['eid'].isin(eids)].copy()
            dfj = gsjs[gsjs['sid'].isin(sids)].copy()
            rec = dfe.iloc[0]
            chrom = rec['chr']
            if self.strand=='+':
                st = dfe[dfe['eid']==e5id]['ed'].values[0]
            else:
                st = dfe[dfe['eid']==e5id]['st'].values[0]
            id5 = '{0}:{1}:{2}'.format(chrom,st,self.strand)
            dfe['id5'] = id5
            dfj['id5'] = id5
            g5s.append((dfj, dfe))
        return g5s

    def get_53groups(self, eid):
        # find exons and juncs which are part of the subgraph
        # who shares start exon (e5id) and end exon (e3id)
        # : do tree walk and record which 3'exons each exon can lead to
        e2leaves = {}
        ede = self.ede
        to_visit = [eid]
        while(len(to_visit)>0):
            e = to_visit.pop(-1)
            # already done?
            if e in e2leaves:
                continue
            if len(ede.get(e,[]))==0:#e not in ede: # leaf
                e2leaves[e] = [e]
            else:
                children = ede[e]
                if any([x not in e2leaves for x in children]):
                    to_visit += [e]+children
                else:
                    e2leaves[e] = list(set([x for y in children for x in e2leaves[y]]))
        return e2leaves[eid], e2leaves

    def find_53groups(self, gsjs, gexs):
        g53s = []
        ex5s = gexs[gexs['kind']=='5'] # heads
        j2 = self.j2
        for e5id in ex5s['eid']:
            e3ids, e2leaves = self.get_53groups(e5id)
            for e3id in e3ids:
                eids = [x for x in e2leaves if e3id in e2leaves[x]]
                sids = j2[(j2['eid1'].isin(eids))&(j2['eid2'].isin(eids))]['sid'].values
                dfe = gexs[gexs['eid'].isin(eids)].copy()
                dfj = gsjs[gsjs['sid'].isin(sids)].copy()
                rec5 = dfe[dfe['eid']==e5id].iloc[0]
                rec3 = dfe[dfe['eid']==e3id].iloc[0]
                chrom = rec5['chr']
                if self.strand=='+':
                    st = rec5['ed']
                    ed = rec3['st']
                else:
                    st = rec3['ed']
                    ed = rec5['st']
                id53 = '{0}:{1}-{2}:{3}'.format(chrom,st,ed,self.strand)
                dfe['id53'] = id53
                dfj['id53'] = id53
                dfe['_e5id'] = e5id
                dfe['_e3id'] = e3id
                dfe['tst'] = st
                dfe['ted'] = ed
                dfj['tst'] = st
                dfj['ted'] = ed
                g53s.append((dfj, dfe))
        return g53s        

####### Bundle Finder ################################################################
    
def find_gaps(bwpre, chrom, csize, gsizeth=5e5, minbundlesize=10e6, sjbwpre=None, sjth=0):
    sjexbw = SjExBigWigs(bwpre, sjbwpre, mixunstranded=False)
    sts = []
    eds = []
    bsize = 2*minbundlesize
    bnum = int(N.ceil(csize/float(bsize)))
    with sjexbw:
        for i in range(bnum):
            st = i*bsize
            ed = min((i+1)*bsize, csize)
            arr = sjexbw.bws['sj']['+'].get(chrom,st,ed)
            arr += sjexbw.bws['sj']['-'].get(chrom,st,ed)
            idx = N.nonzero(arr<=sjth)[0]
            if len(idx)==0:
                continue
            dif = idx[1:]-idx[:-1] # if continuous dif==1
            idx2 = N.nonzero(dif>1)[0] # non contiguous    
            # gap start idx2[x]+1 ==> gap end idx2[x+1]
            gsize = idx[idx2[1:]]-idx[idx2[:-1]+1]
            idx3 = N.nonzero(gsize>gsizeth)[0]
            gst = idx[idx2[idx3]+1]+st
            ged = idx[idx2[idx3+1]]+st
            sts += list(gst)
            eds += list(ged)
    return sts,eds

def find_bundles(bwpre, genome, dstpre, chrom=None, sjbwpre=None, mingap=5e5, minbundlesize=10e6, sjth=0):
    bundles = []
    if chrom is None:
        chroms = UT.chroms(genome) # whole genome
        fpath = dstpre+'.bundles.txt.gz'
        if os.path.exists(fpath):
            df = UT.read_pandas(fpath)
            return df[['chr','st','ed']].values
    else:
        chroms = [chrom]
        fpath = dstpre+'.{0}.bundles.txt.gz'.format(chrom)
        if os.path.exists(fpath):
            df = UT.read_pandas(fpath)
            return [tuple(x) for x in df[['chr','st','ed']].values]
    chromsizes = UT.df2dict(UT.chromdf(genome), 'chr', 'size')
    for chrom in chroms:
        print('checking {0}...'.format(chrom))
        csize = chromsizes[chrom]
        sts,eds = find_gaps(bwpre, chrom, csize, mingap, minbundlesize,sjbwpre,sjth)
        st = 0
        if len(sts)==0:
            bundles.append((chrom,0,csize))
        else:
            for gs,ge in zip(sts,eds):
                mid = int((gs+ge)/2.)
                if mid-st>minbundlesize:
                    bundles.append((chrom,st,mid))
                    st = mid
            if ge<csize:
                bundles.append((chrom,st,csize))
    df = PD.DataFrame(bundles, columns=['chr','st','ed'])
    UT.write_pandas(df, fpath, 'h')
    return bundles

######### Chrom Assembler ###########################################################


def bundle_assembler(bwpre, chrom, st, ed, dstpre, laparams={}, sjbwpre=None, refcode='gen9'):
    bname = bundle2bname((chrom,st,ed))
    bsuf = '.{0}_{1}_{2}'.format(chrom,st,ed)
    csuf = '.{0}'.format(chrom)
    LOG.info('assembling bunle {0}'.format(bname))
    sufs = ['.exdf.txt.gz',
            '.sjdf.txt.gz',
            '.paths.txt.gz',
            '.paths.bed.gz',
            '.tspans.bed.gz',
            '.unused.sjpath.bed.gz']
    done = []
    for x in sufs:
        done.append(os.path.exists(dstpre+bsuf+x) | \
                    os.path.exists(dstpre+csuf+x) | \
                    os.path.exists(dstpre+x) )
    if all(done):
        return bname

    classifierpre = bwpre+'.'+refcode
    la = LocalAssembler(bwpre, chrom, st, ed, dstpre, 
        classifierpre=classifierpre,
        sjbwpre=sjbwpre, 
        **laparams, 
        )
    return la.process()

def bname2bundle(bname):
    # bname = 'chrom:st-ed'
    chrom, sted = bname.split(':')
    st,ed = [int(x) for x in sted.split('-')]
    return chrom,st,ed

def bundle2bname(b):
    return '{0}:{1}-{2}'.format(*b)

def chrom_assembler(bwpre, dstpre, genome, chrom, mingap=5e5, minbundlesize=10e6, np=2):
    bundles = find_bundles(bwpre, genome, dstpre, chrom, mingap=mingap, minbundlesize=minbundlesize)
    print('{1}: #bundles: {0}'.format(len(bundles), chrom))
    server = TQ.Server(np=np)
    with server:
        for c,st,ed in bundles:
            tname = 'bundle_assembler.{0}:{1}-{2}'.format(c,st,ed)
            args = (bwpre, c, st, ed, dstpre)
            task = TQ.Task(tname, bundle_assembler, args)
            server.add_task(task)
    rslts = {}
    n = len(bundles)
    bundlestatus = {}
    for i in range(n):
        try:
            name,ans = server.get_result(True, 10) # block, r: (value, name)
            bname = name.split('.')[1]
            rslts[bname2bundle(bname)] = ans
            bundlestatus[bname] = ans
        except multiprocessing.Queue.Empty:
            pass
    bundles1 = []
    for x in bundles:
        if rslts.get(tuple(x),None) is None:
            print('no results from bundle {0}'.format(bundle2bname(x)))
        else:
            bundles1.append(x)
    concatenate_bundles(bundles1, bundlestatus, chrom, dstpre)
    return '{0}.{1}'.format(dstpre,chrom)

def concatenate_bundles(bundles, bundlestatus, chrom, dstpre):
    # concat results
    sufs = ['exdf.txt.gz', 
           'sjdf.txt.gz',
           'paths.txt.gz',
           'paths.bed.gz',
           'tspans.bed.gz',
           'unused.sjpath.bed.gz']
    files = []
    for suf in sufs:
        dstpath = '{0}.{1}.{2}'.format(dstpre, chrom, suf)
        dstpath2 = '{0}.{1}'.format(dstpre, suf)
        if not os.path.exists(dstpath2):
            if not os.path.exists(dstpath):
                with open(dstpath, 'wb') as dst:
                    for chrom, st, ed in bundles:
                        bname = bundle2bname((chrom,st,ed))
                        srcpath = '{0}.{1}_{2}_{3}.{4}'.format(dstpre, chrom, st, ed, suf)
                        if not os.path.exists(srcpath):
                            if bundlestatus[bname] is None:
                                continue
                            else:
                                raise RuntimeError('{0} does not exists'.format(srcpath))
                        files.append(srcpath)
                        with open(srcpath, 'rb') as src:
                            shutil.copyfileobj(src, dst)
        else:
            files+=['{0}.{1}_{2}_{3}.{4}'.format(dstpre, chrom, st, ed, suf) for chrom,st,ed in bundles]
    # cleanup
    for f in files:
        if os.path.exists(f):
            os.unlink(f)

def concatenate_chroms(chroms, dstpre):
    # concat results
    sufs = ['exdf.txt.gz', 
           'sjdf.txt.gz',
           'paths.txt.gz',
           'paths.bed.gz',
           'tspans.bed.gz',
           'unused.sjpath.bed.gz']
    files = []
    for suf in sufs:
        dstpath = '{0}.{1}'.format(dstpre, suf)
        if not os.path.exists(dstpath):
            with open(dstpath, 'wb') as dst:
                for chrom in chroms:
                    srcpath = '{0}.{1}.{2}'.format(dstpre, chrom, suf)
                    if os.path.exists(srcpath):
                        files.append(srcpath)
                        with open(srcpath, 'rb') as src:
                            shutil.copyfileobj(src, dst)
        else:
            files+=['{0}.{1}.{2}'.format(dstpre, chrom, suf) for chrom in chroms]
    # cleanup
    for f in files:
        if os.path.exists(f):
            os.unlink(f)

def write_stats(dstpre, seinfo):
    dic = {}
    dic.update(seinfo)
    exdf = UT.read_pandas(dstpre+'.exdf.txt.gz', names=EXDFCOLS)
    dic['num_me_exons'] = len(exdf)
    sjdf = UT.read_pandas(dstpre+'.sjdf.txt.gz', names=SJDFCOLS)
    dic['num_junctions'] = len(sjdf)
    paths = UT.read_pandas(dstpre+'.paths.txt.gz', names=PATHCOLS)
    dic['num_paths'] = len(paths)
    unused = GGB.read_bed(dstpre+'.unused.sjpath.bed.gz')
    dic['num_unused_junctions'] = len(unused)
    fname = dstpre+'.stats.txt'
    name = dstpre.split('/')[-1]
    df = PD.DataFrame(dic, index=[name])
    LOG.info('{0}:{1}'.format(name, dic))
    UT.write_pandas(df, fname, 'ih')
    

######### Sample Assembler ###########################################################

def sample_assembler(bwpre, dstpre, genome, mingap=5e5, minbundlesize=10e6, np0=2, np1=2, chroms=None):
    server = TQ.Server(np=np0)
    if chroms is None:
        chroms = UT.chroms(genome)
    with server:
        for chrom in chroms:
            tname = 'chrom_assembler.{0}'.format(chrom)
            args = (bwpre, dstpre, genome, chrom, mingap, minbundlesize, np1)
            task = TQ.Task(tname, chrom_assembler, args)
            server.add_task(task)
    rslts = {}
    n = len(chroms)
    for i in range(n):
        try:
            name,ans = server.get_result(True, 10) # block, r: (value, name)
            c = name.split('.')[1]
            rslts[c] = ans
        except multiprocessing.Queue.Empty:
            pass
    chroms1 = []
    for x in chroms:
        if rslts.get(x,None) is None:
            print('no results from {0}'.format(x))
        else:
            chroms1.append(x)    
    concatenate_chroms(chroms1, dstpre)
    return dstpre


def find_SE_chrom(bwpre, dstpre, genome, chrom, exstrands=['+'], minsizeth=200):
    # find SE candidates and calculate ecovs
    try:
        exdf = UT.read_pandas(dstpre+'.{0}.exdf.txt.gz'.format(chrom), names=EXDFCOLS)
    except:
        exdf = UT.read_pandas(dstpre+'.exdf.txt.gz', names=EXDFCOLS)
    exdf = exdf[exdf['chr']==chrom]
    sjexbw = SjExBigWigs(bwpre)
    chromdf = UT.chromdf(genome).set_index('chr')
    csize = chromdf.ix[chrom]['size']
    def _do_strand(strand):
        with sjexbw:
            exa = sjexbw.bws['ex'][strand].get(chrom,0,csize)
        # mask existing exons
        for st,ed in exdf[['st','ed']].values:
            exa[st:ed] = 0
        idx = N.nonzero(exa>0)[0]
        # find continuous segments
        dif = idx[1:]-idx[:-1] # if continuous dif==1
        idx2 = N.nonzero(dif>1)[0] # non contiguous   
        idxst = N.array([idx[0]]+list(idx[idx2+1]))
        idxed = N.array(list(idx[idx2])+[idx[-1]])
        # gap start idx2[x]+1 ==> gap end idx2[x+1]
        gsize = idxed - idxst + 1
        idx3 = N.nonzero(gsize>minsizeth)[0]
        st = idxst[idx3]
        ed = st + gsize[idx3]
        df = PD.DataFrame({'st':st, 'ed':ed}, index=N.arange(len(st)))
        df['ecov'] = [N.mean(exa[x:y]) for x,y in df[['st','ed']].values]
        df['len'] = df['ed']-df['st']
        df['chr'] = chrom
        return df
    df = PD.concat([_do_strand(x) for x in exstrands],ignore_index=True)
    df = df.groupby(['st','ed']).first().reset_index()
    dstpath = dstpre+'.se.{0}.{1}.txt.gz'.format(chrom,'.'.join(exstrands))
    UT.write_pandas(df[['chr','st','ed','ecov','len']], dstpath, '')
    return dstpath

def find_SE(dstpre, chroms, exstrands=['+'], sestrand='.', 
    mincovth=5, minsizeth=200, minsep=1000, cmax=9, mergedist=200):
    # concatenate
    dstpath = dstpre+'.se0.txt.gz'
    if not os.path.exists(dstpath):
        with open(dstpath,'wb')  as dst:
            for chrom in chroms:
                srcpath = dstpre+'.se.{0}.{1}.txt.gz'.format(chrom,'.'.join(exstrands))
                with open(srcpath,'rb') as src:
                    shutil.copyfileobj(src,dst)
                os.unlink(srcpath)
    # concatenate 
    if not os.path.exists(dstpre+'.exdf.txt.gz'):
        concatenate_chroms(chroms, dstpre)
    secols = ['chr','st','ed','ecov','len']
    sedf = UT.read_pandas(dstpath, names=secols)
    exdf = UT.read_pandas(dstpre+'.exdf.txt.gz', names=EXDFCOLS) 
    # th = find_threshold(exdf['ecov'].values, sedf['ecov'].values, mincovth, dstpre)
    paths = UT.read_pandas(dstpre+'.paths.txt.gz', names=PATHCOLS)
    th = find_threshold(paths['tcov'].values, sedf['ecov'].values, mincovth, dstpre)
    se0 = sedf[(sedf['ecov']>th)&(sedf['len']>minsizeth)].copy()  # use FPR 1%

    LOG.info('SE covth={0:.2f}, len(se0)={1}'.format(th, len(se0)))
    # se0['strand'] = sestrand
    # se0['name'] = [_pc(st,ed,sestrand,',') for st,ed in se0[['st','ed']].values ]
    # se0['kind'] = 's'
    # c = dstpre+'.se1.txt.gz'
    # UT.write_pandas(se0, c, 'h')
    # return (c, th, len(sedf), len(se0))
    # check irets in bundle workers?
    # find intervals at least minsep separated from exons
    a = dstpre+'.se0.bed'
    b = dstpre+'.espans.bed'
    # cols = ['chr','st','ed','ecov','strand']
    se0 = se0[['chr','st','ed','ecov']].sort_values(['chr','st','ed'])
    UT.write_pandas(se0, a, '')
    exdf['st0'] = exdf['st']-minsep
    exdf['ed0'] = exdf['ed']+minsep
    UT.write_pandas(exdf[['chr','st0','ed0']],b,'')
    c0 = dstpre+'.sedf0.txt.gz'
    BT.bedtoolintersect(a,b,c0,v=True)
    # merge nearby 
    c = dstpre+'.sedf.txt.gz'
    BT.bedtoolmerge(c0,c,d=mergedist,c=4,o='mean')
    se1a = UT.read_pandas(c0,names=['chr','st','ed','ecov'])
    se1 = UT.read_pandas(c, names=['chr','st','ed','ecov'])
    se1['strand'] = sestrand
    se1['name'] = [_pc(st,ed,sestrand,',') for st,ed in se1[['st','ed']].values ]
    se1['kind'] = 's'
    UT.write_pandas(se1[EXDFCOLS], c, '')

    cbed = dstpre+'.se.bed.gz'
    GGB.write_bed(se1, cbed, ncols=3)
    LOG.info('#SE = {0} (before merge {1})'.format(len(se1), len(se1a)))
    os.unlink(a)
    os.unlink(b)
    os.unlink(c0)
    # merge exdf & se ==> update .exdf.txt.gz?
    # update .paths.txt.gz, .paths.bed.gz?
    bed = GGB.read_bed(dstpre+'.paths.bed.gz') # chr,st,ed,name,sc1,strand,sc2,tst,ted,#exons,esizes,estarts

    # BED12
    se1['ltcov'] = N.log2(se1['ecov']+2)
    se1['sc1'] = N.ceil(se1['ltcov']*100).astype(int)
    sm = {'+':Colors('R', cmax),
          '-':Colors('B', cmax),
          '.':Colors('C', cmax)}
    se1['sc2'] = [sm[s].RGB(x) for x,s in se1[['ltcov','strand']].values]
    se1['tst'] = se1['st']
    se1['ted'] = se1['ed']
    se1['#exons'] = 1
    se1['len'] = se1['ed']-se1['st']
    se1['esizes'] = se1['len'].astype(str)+','
    se1['estarts'] = '0,'
    bed1 = PD.concat([bed, se1], ignore_index=True)
    bed1['st'] = bed1['st'].astype(int)
    bed1['ed'] = bed1['ed'].astype(int)
    bed1['tst'] = bed1['tst'].astype(int)
    bed1['ted'] = bed1['ted'].astype(int)
    GGB.write_bed(bed1, dstpre+'.paths.withse.bed.gz', ncols=12)
    dic = dict(
        secovth=th, 
        num_se_candidates=len(sedf), 
        num_se_by_covth_and_size=len(se0), 
        num_se_not_near_exons=len(se1a),
        num_se_merge_nearby=len(se1))
    return dic

def find_threshold(x0,x1,minth,dstpre,fdrth=0.5, fprth=0.01):
    x0 = x0[(~N.isnan(x0))&(x0>0)]  # why exdf contains NaN?
    x1 = x1[(~N.isnan(x1))&(x1>0)]
    x0 = N.log2(x0+1)
    x1 = N.log2(x1+1)
    xmax = min(x0.max(), x1.max())
    xmin = max(x0.min(), x1.min())
    delta = (xmax-xmin)/100.
    bins=N.arange(xmin,xmax,delta)
    h0,b0 = N.histogram(x0, bins=bins)
    h1,b1 = N.histogram(x1, bins=bins)
    # def find_best_xmid(h0,h1):
    deltas = []
    for i in range(len(h0)-25):
        scale = float(N.sum(h1[i:]))/N.sum(h0[i:])
        h0s = h0*scale
        delta = N.mean(N.abs(h0s[i:]-h1[i:]))
        deltas.append([delta, i, h0s, scale])
    delta,posi,h0s,scale =sorted(deltas)[0]
    cntf = h0s
    cnto = h1
    cp = N.sum(cntf)   # total positive
    cn = N.sum(cnto)-cp # total negative (observed - positive)
    if cn<0: # due to noise when almost perfect
        th_fpr = minth
        th_fdr = minth
    else:
        fn = N.cumsum(cntf) # false negative (condition positive but < th)
        tp = cp - fn # true positive (condition positive and >= th)
        tn = N.cumsum(cnto) - fn  # true negative
        tn[tn<0]=N.nan
        fp = cn - tn
        fp[fp<0]=N.nan
        #tpr = tp/cp
        fpr = fp/cn
        p = N.sum(cnto)-N.cumsum(cnto) # declared positive
        fdr = fp/p
        fpr[N.isnan(fpr)]=0
        idx = N.nonzero(fdr<=fdrth)[0]
        if len(idx)==0: # not found
            th_fdr = minth
        else:
            th_fdr = 2**(bins[N.min(idx[idx>10])])-1
        idx0 = N.nonzero(fpr<=fprth)[0]
        if len(idx0)==0: # not found
            th_fpr = minth
        else:
            th_fpr = 2**(bins[N.min(idx0[idx0>10])])-1
    fname = dstpre+'.secovth.pdf'
    title = dstpre.split('/')[-1]
    plot_se_th(b0,h0s,h1,th_fpr,th_fdr,title,fname)
    return th_fpr


def plot_se_th(b0,h0s,h1,th0,th,title,fname=None):
    fig,ax = P.subplots(1,1)
    w = b0[1]-b0[0]
    ax.bar(b0[:-1], h1, width=w, alpha=0.9,color='c', label='single-exon', lw=0)
    ax.bar(b0[:-1], h0s, width=w, alpha=0.5, color='r', label='multi-exon', lw=0)
    ax.set_yscale('log')
    ax.axvline(N.log2(th+1), color='r', linestyle='--', label='FDR 50%')
    ax.axvline(N.log2(th0+1), color='b', linestyle='--', label='FPR 1%')
    ax.set_xlabel('log2(cov+1)')
    ax.set_ylabel('count')
    ax.set_title(title)
    ax.legend()
    if fname is not None:
        fig.savefig(fname)
    

def find_threshold0(x0,x1,minth):
    x0 = x0[~N.isnan(x0)]  # why exdf contains NaN?
    x1 = x1[~N.isnan(x1)]
    x0 = N.log2(x0+1)
    x1 = N.log2(x1+1)
    xmax = min(x0.max(), x1.max())
    xmin = max(x0.min(), x1.min())
    delta = (xmax-xmin)/25.
    bins=N.arange(xmin,xmax,delta)
    h0,b0 = N.histogram(x0, bins=bins)
    h1,b1 = N.histogram(x1, bins=bins)
    xmid = 0.6*(xmax-xmin)+xmin
    scale = float(N.sum(x1>xmid))/N.sum(x0>xmid)
    h0s = scale*h0
    d0 = N.abs(h1-h0s)/h0s
    d0s = smooth(d0,5)
    d0sd = d0s[1:]-d0s[:-1]
    d0sdd = d0sd[1:]-d0sd[:-1]
    for i in range(4,len(d0sdd)):
        if d0sdd[i-1]*d0sdd[i]<0:
            break
    th = b0[1:][i]
    if th<N.log2(minth+1):
        return minth
    if th>0.8*xmax:
        LOG.warning('find_threshold: threshold too large {0} returning 0.8*xmax {1}'.format(th,0.7*xmid))
        return 2**(0.8*xmax)-1
    return 2**th-1


def smooth( v, wsize):
    swin = N.ones(wsize)
    v0 = N.concatenate([swin*v[0], v, swin*v[-1]])
    return N.convolve(v0, swin/float(wsize), 'same')[wsize:-wsize]



SEPARAMS = dict(
    sestrand='.',
    exstrands=['+'], 
    mincovth=5, 
    minsizeth=200, 
    minsep=1000, 
    cmax=9, 
    mergedist=200
)
BUNDLEPARAMS = dict(
    sjth=0,
    mingap=1e5, 
    minbundlesize=20e6, 
)

class SampleAssembler(object):

    def __init__(self, bwpre, dstpre, genome, 
        sjbwpre=None,
        refcode='gen9',
        np=4, 
        chroms=None, 
        maxwaittime=600,
        bundleparams={},
        separams={},
        laparams={},
        ):
        self.bwpre = bwpre
        self.dstpre = dstpre
        self.genome = genome
        self.sjbwpre = sjbwpre
        self.np = np
        self.chroms = chroms
        if self.chroms is None:
            self.chroms = UT.chroms(genome)
        self.maxwaittime = maxwaittime # if worker doesn't return within this time limit something is wrong

        self.bundleparams = BUNDLEPARAMS.copy()
        self.bundleparams.update(bundleparams)
        self.separams = SEPARAMS.copy()
        self.separams.update(separams)
        self.laparams = LAPARAMS.copy()
        self.laparams.update(laparams)
        self.refcode = refcode

    def run(self):
        self.server = server = TQ.Server(np=self.np)
        self.bundles = bundles = {} # chr => [(chr,st,ed),...]
        self.bundlestatus = bundlestatus = {} # chrom => bundle (chr,st,ed) => done status
        self.chromstatus = chromstatus = {}
        self.find_se_chrom_status = find_se_chrom_status = {}

        with server:
            for chrom in self.chroms:
                tname = 'find_bundle.{0}'.format(chrom)
                args = (self.bwpre, self.genome, self.dstpre, chrom, self.sjbwpre)
                task = TQ.Task(tname,find_bundles, args, self.bundleparams)
                server.add_task(task)
            while server.check_error(self.maxwaittime): # loop
                try:
                    name, rslt = server.get_result(timeout=5) # block until result come in
                except TQ.Empty:
                    name, rslt = None, None
                if name is not None:
                    if name.startswith('find_bundle.'):
                        print('{0}:{1}'.format(name, len(rslt)))
                        chrom = name.split('.')[1]
                        bundles[chrom] = rslt
                        for c,st,ed in rslt:
                            # print('put task##bundle_assembler {0}:{1}-{2}'.format(chrom,st,ed))
                            tname = 'bundle_assembler.{0}:{1}-{2}'.format(c,st,ed)
                            # bwpre, chrom, st, ed, dstpre, laparams={}, sjbwpre=None, refcode='gen9'
                            args = (self.bwpre, c, st, ed, self.dstpre, self.laparams, self.sjbwpre, self.refcode)
                            task = TQ.Task(tname, bundle_assembler, args)
                            server.add_task(task)
                    if name.startswith('bundle_assembler.'):
                        bname = name.split('.')[1]
                        chrom = bname.split(':')[0]
                        bundlestatus.setdefault(chrom,{})[bname] = rslt
                        if len(bundlestatus[chrom])==len(bundles[chrom]): # all done
                            # print('put task##concatenate_bundles {0}'.format(chrom))
                            tname = 'concatenate_bundles.{0}'.format(chrom)
                            # bundles, bundlestatus, chrom, dstpre
                            args = (bundles[chrom], bundlestatus[chrom], chrom, self.dstpre)
                            task = TQ.Task(tname, concatenate_bundles, args)
                            server.add_task(task)
                    if name.startswith('concatenate_bundles.'):
                        chrom = name.split('.')[1]
                        chromstatus[chrom] = rslt
                        # start SE finder for chrom
                        # print('put task##find_SE_chrom {0}'.format(chrom))
                        tname = 'find_SE_chrom.{0}'.format(chrom)
                        # bwpre, dstpre, genome, chrom, exstrand='+', minsizeth=200
                        exstrands = self.separams['exstrands']
                        minsizeth = self.separams['minsizeth']
                        args = (self.bwpre, self.dstpre, self.genome, chrom, exstrands, minsizeth)
                        task = TQ.Task(tname, find_SE_chrom, args)
                        server.add_task(task)
                    if name.startswith('find_SE_chrom.'):
                        chrom = name.split('.')[1]
                        find_se_chrom_status[chrom] = rslt
                        if len(find_se_chrom_status)==len(self.chroms):
                            # print('start SE finder')
                            tname = 'find_SE'
                            # dstpre, chroms, exstrand='+', sestrand='.', mincovth=5, minsizeth
                            # dstpre, chroms + separams
                            args = (self.dstpre, self.chroms)
                            task = TQ.Task(tname, find_SE, args, self.separams)
                            server.add_task(task)
                    if name== 'find_SE':
                        tname = 'write_stats'
                        args = (self.dstpre, rslt)
                        task = TQ.Task(tname, write_stats, args)
                        server.add_task(task)
                    if name=='write_stats':
                        break
                        # tname = 'concatenate_chroms'
                        # args = (self.chroms, self.dstpre)
                        # task = TQ.Task(tname, concatenate_chroms, args)
                        # server.add_task(task)
                    # if name== 'concatenate_chroms':
                    #     break
            print('Exit Loop')
        print('Done')







