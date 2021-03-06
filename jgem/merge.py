"""

.. module:: merge
    :synopsis: module for merging multiple assemblies

..  moduleauthor:: Ken Sugino <ken.sugino@gmail.com>

"""

import subprocess
import os
import gzip
import logging
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)
import shutil
import json

import pandas as PD
import numpy as N

from jgem import utils as UT
from jgem import gtfgffbed as GGB
from jgem import filenames as FN
from jgem import assembler as AS
from jgem import bedtools as BT
from jgem import bigwig as BW
from jgem import calccov as CC
from jgem import convert as CV

MERGECOVPARAM = dict(
    np = 1, # number of CPU to use
    genome = 'mm10', # UCSC genome name

    # make ex bed param
    covth = 0, # exon read digitization threshold
    covdelta = 1, # exon read digitization unit
    # make ex.se bed param
    minsecovth=30, # min secov at individual sample level for SE to be included
    secovfactor=3, # *secovth is the threshold to include  

    # deprecated
    th_maxcnt=1, # max read count should be larger than this
    th_maxcnt2=20, # if maxcnt larger than this ignore th_detected

    # select_sj params
    uth=0, # unique count threshold
    mth=100, # non-unique count threshold
    th_detected=2, # observed in more than th_detected samples
    th_maxoverhang=15, # min maxoverhang
    th_ratio=1e-3, # discard junctions less than this portion within overlapping junctions
    i_detected=1, # intercept for #detected
    i_maxcnt=1, # intercept for maxcnt

    # for remove_jie (version one)
    jie_binth=10, # 10x average cov
    jie_sjth=0.1, #   0.1x
    jie_ovlth=0.95, # how much overlap to high coverage interval?
    # for remove_jie verion 2, calculate sj threshod from coverage of the interval
    jie_acovfactor=1, # binth = this x acov
    jie_sjfactor=1e-3, # sjth = this x coverage (of the interval), a bit too low? 2e-3?

)
MERGEASMPARAM = dict(
    # se_maxth=500,   # SE maxcov threshold
    se_gidstart=50000, # SE gidx start
    minsecovth=30, # min secov at individual sample level for SE to be included
    # secovfactor=3, # *secovth is the threshold to include   
    se_binth=10, # when extracting SE candidates from allsample.bw
    sebinth_factor=10, # sebinth = averagecov/sebinth_factor
    use_se2=False, # add high cov SE from each sample?
    se_minsize=100, # min size for SE
    se_maxsize=10000, # in gen4 only one SE is larger than 10Kbp
    secov_fpr_th=0.01, # FPR threshold
    # jie_binth=10, # => moved
    ureadth=0,
    mreadth=200,
    findsecov_usesemax=True,
    do_selectseme=False,
    do_mergeexons=False,
)


class MergeInputNames(FN.FileNamesBase):
    """Filelname manager for generating inputs for merging process.

    Attributes:
        sampleinfo: sample info dataframe (with columns: name, sjpath, expath, bwfile, sjfile)
        code: merge identifier (for merge input generation part)
        outdir: output directory

    All outputs and temporary files are prefixed by **outdir/code**

    SampleInfo Columns:
        name: sample name (unique)
        sjexpre: path prefix to SJ/EX files (sj.txt.gz, ex.txt.gz will be added)
        bw_path: original bigwig coverage
        sjbed_path: original junction bed file (converted from SJ.out.tab)

    """

    def __init__(self, sampleinfo, code, outdir, checkfiles=True):
        self.si = sampleinfo
        self.code = code
        self.outdir = outdir
        # check required fields in sampleinfo
        sicols = sampleinfo.columns
        for c in ['sjbed_path','bw_path','sjexpre','name']:
            if c not in sicols:
                raise ValueError('{0} not in sampleinfo'.format(c))
        # check existence of files
        if checkfiles:
            for f in sampleinfo['sjbed_path'].values:
                if not os.path.exists(f):
                    raise ValueError('file {0} does not exists'.format(f))
            for f in sampleinfo['bw_path'].values:
                if not os.path.exists(f):
                    raise ValueError('file {0} does not exists'.format(f))
            for f in sampleinfo['sjexpre'].values:
                for suf in ['.ex.txt.gz','.sj.txt.gz']:
                    if not os.path.exists(f+suf):
                        raise ValueError('file {0} does not exists'.format(f+suf))

        prefix = os.path.join(outdir, code)
        super(MergeInputNames, self).__init__(prefix)

    def expaths(self):
        return [(n, '{0}.ex.txt.gz'.format(x)) for n,x in self.si[['name','sjexpre']].values]

    def sjopaths(self):
        return [(n, '{0}.sj.txt.gz'.format(x)) for n,x in self.si[['name','sjexpre']].values]

    def sjpaths(self):
        return self.si[['name','sjbed_path']].values # BED

    def bwpaths(self):
        return self.si[['name','bw_path']].values # BIGWIG

    def bwpres(self):
        return self.si[['name','bwpre']].values # BIGWIG

    def sj0_bed(self):
        return self.bedname('sj0', category='output')

    def sj5_txt(self):
        return self.txtname('sj5', category='output')

    def sj6_txt(self):
        return self.txtname('sj6', category='output')

    def sj1_txt(self):
        return self.txtname('sj1', category='output')

    def sj_bed(self, strand):
        """SJ output, strand = p,n """
        return self.bedname('sj.{0}'.format(strand), category='output')

    def sj2_txt(self):
        """intermediate output with selection parameters """
        return self.txtname('sj2', category='output')

    def allsj_txt(self):
        return self.txtname('allsj', category='output')

    def allsjT_txt(self):
        return self.txtname('allsj.T', category='output')

    def allsj_stats(self):
        return self.txtname('allsj.stats', category='output')

    def ex_bw(self, k):
        """BW output, strand = p,n """
        return self.fname('ex.{0}.bw'.format(k), category='output')

    def ex_bed(self, k):
        return self.bedname('ex.{0}'.format(k))

    def agg_bw(self):
        return self.fname('allsample.bw', category='output')

    def agguniq_bw(self):
        return self.fname('allsample.uniq.bw', category='output')

    def snames(self):
        return list(self.si['name'])

class MergeAssemblyNames(FN.FileNamesBase):
    """Filelname manager for the assembling part of the merging process.

    Attributes:
        code: merge identifier (for merge input generation part)
        outdir: output directory
        refgtf: reference gtf path if using it for finding SE cov threshold

    All outputs and temporary files are prefixed by **outdir/code**

    """

    def __init__(self, code, outdir, refgtf='.gtf'):
        self.code = code
        self.outdir = outdir
        # self.refgtf = refgtf
        self.refgtf = CV.GTF2SJEX(refgtf)

        prefix = os.path.join(outdir, code)
        super(MergeAssemblyNames, self).__init__(prefix)

    def ex_out(self, ext='txt'):
        return self.fname('ex.{0}.gz'.format(ext), category='output')

    def sj_out(self, ext='txt'):
        return self.fname('sj.{0}.gz'.format(ext), category='output')

    def ci_out(self):
        return self.fname('ci.txt.gz', category='output')

    def genes_out(self, ext='txt'):
        return self.fname('genes.{0}.gz'.format(ext), category='output')


class MergeInputs(object):
    """Generates inputs to merge assembling. Creates multiple bigwig files and a junction file.
    Multiple bigwig file are for exons in each strand and for single exons. 

    1. pepare 3 merged bigwigs (ME+/ME-/SE) from all assemblies
    2. prepare an aggregated junction file
    3. select junctions
    4. calculate average bigwig from original bigwigs

    (Note: at 1. bigwigs are from exon model outputs from each assembly, at 4. bigwig is just an
    average of all original bigiwigs which were inputs to each assembly.)
    """
    def __init__(self, fnobj, genome='mm10', **kw):
        """
        Args:
            fnobj: MergeInputNames object

        Other keywords arguments:
            * genome: UCSC genome name
            * np: number of CPU to use
            * covth: only use exons with coverage > covth (default 0)
            * covdelta: exon coverage quantization delta
            * uth: unique reads threshold for junctions (default 0)
            * mth: non-unique reads threshold for junctions (default 5)
            * th_ratio: threshold for selecting junctions by overlapping ratio 
              (default 0.001, i.e. if a junction's read is less than 1/1000 of the sum of 
              all the reads of overlapping junctions then the junction is discarded)
            * th_detected: unctions need to be detected in more than this number of samples 
              (default 1)
            * th_maxcnt: max junction reads across sample has to be larger than this
              (default 100)

        """
        self.fnobj = fnobj
        self.params = MERGECOVPARAM.copy()
        self.params.update(kw)
        self.params['genome'] = genome
        self.stats = {}
        self.chromsizes = UT.chromsizes(genome)
        self.chroms = UT.chroms(genome)
        self.tgts =  ['mep','men','se'] #'sep','sen']


    def save_params(self):
        """ Saves merge parameters with filename outdir/mergecode.params.json """
        fname1 = self.fnobj.fname('params.json',category='output')
        UT.makedirs(os.path.dirname(fname1))
        with open(fname1,'w') as fp:
            json.dump(self.params, fp)
        fname2 = self.fnobj.fname('stats.txt',category='output')
        statdf = PD.DataFrame(self.stats, index=['value']).T
        UT.write_pandas(statdf, fname2, 'ih')

    def prepare(self):
        """ Prepare merged bigwig coverage files, merged junction files and aggregated bigwig file (average cov)."""
        self.make_ex_bigwigs()
        self.make_sj_bed()
        self.aggregate_bigwigs()
        self.fnobj.delete(delete=['temp'],protect=['output'])
        self.save_params()

    def make_ex_bigwigs(self):
        """Make 5 bigwig files from assembly exon outputs by treating each exon as reads weighted by coverage. """
        fn = self.fnobj
        # first make BED files
        LOG.debug('making BEDS...')
        self._make_ex_beds()
        # then convert them to BIGWIGs
        for k in self.tgts:
            bedpath = fn.ex_bed(k) #fn.fname('ex.{0}.bed.gz'.format(k))
            bwpath = fn.ex_bw(k) #fn.fname('ex.{0}.bw'.format(k), category='output')
            LOG.debug('converting {0} to BIGWIG...'.format(bedpath))
            #totbp,covbp = BT.get_total_bp_bedfile(bedpath, bed12=False)
            #scale = float(covbp)/totbp # normalize average coverage to 1.
            #LOG.info('{0}:totbp={1},covbp={2},scale={3}'.format(bedpath,totbp,covbp,scale))
            # scale = 1e8/totbp # old normalization = 1e6/totaligned when readlen=100bp
            # ^==== TODO: Is normalizing to totaligned good? 
            # If complexity (#genes) is bigger then per element cov is smaller. 
            # This will affect the average cov level and noise level. 
            # ====> make average coverage constant

            # [Q] normalize chrom-wise? Mouse chr11,19,X seems higher than average
            #     in addition to the obvious low expressing chrY

            # 2016-04-28: don't scale just reflect read depth all the way through

            #BT.bed2bw(bedpath, self.chromsizes, bwpath, scale=scale)
            BT.bed2bw(bedpath, self.chromsizes, bwpath, scale=None)

        # delete temp files
        fn.delete(delete=['temp'],protect=['output'])

    def _make_ex_beds(self):
        fn = self.fnobj
        pr = self.params
        np = pr['np']
        chroms = self.chroms
        expaths = fn.expaths() # [(name, expath), ...]
        dcode = 'ex.'
        dstpre = fn.fname(dcode)
        tgts = self.tgts # ['mep','men','se'] #sep','sen']
        th = pr['covth']
        delta = pr['covdelta']
        UT.makedirs(os.path.dirname(dstpre))
        mscth = pr['minsecovth']
        scfac = pr['secovfactor']
        args = [(expaths, dstpre, x, th, delta, tgts, mscth, scfac) for x in chroms]

        rslts = UT.process_mp(make_ex_bed_chr, args, np, doreduce=False)

        # concatenate
        LOG.debug('concatenating chroms...')
        UT.makedirs(os.path.dirname(fn.ex_bed(tgts[0])))
        for k in tgts:
            bf = fn.ex_bed(k) #fn.fname('ex.{0}.bed.gz'.format(k))
            with open(bf,'wb') as dst:
                for x in chroms:
                    sf = fn.fname('{0}{1}{2}.gz'.format(dcode,x,k))
                    with open(sf,'rb') as src:
                        shutil.copyfileobj(src, dst)
                        #dst.write(src.read())
        
    def make_sj_bed(self):
        """Make merged junction input file. """
        self.make_sj0_bed() # aggregate junctions
        self.collect_sj() # collect sample junction counts
        self.select_sj() # select junctions ==> do selection in the assembler? (keep it for now )
        self.remove_jie2() # do remove jie here to use info on both strand (inside assmbler strand is separate)
        self.write_sjpn() # write sj.p, sj.n 
        self.fnobj.delete(delete=['temp'],protect=['output'])

    def prepare2(self):
        """ When using previously aggregated data but only changing SJ selection parameters,
        use this function instead of prepare above.

        """
        self.select_sj() # select junctions ==> do selection in the assembler? (keep it for now )
        self.remove_jie2() # do remove jie here to use info on both strand (inside assmbler strand is separate)
        self.write_sjpn() # write sj.p, sj.n 
        self.fnobj.delete(delete=['temp'],protect=['output'])
        self.save_params()

    def make_sj0_bed(self):
        """Aggregate all junctions in the samples. Ucnt, mcnt will be the sum over all samples. """
        pr = self.params
        fn = self.fnobj
        np = pr['np']
        chroms = self.chroms
        sjpaths = fn.sjpaths() # [(name, sjpath),..]
        scode = 'sjbed.gz'
        asjpath = fn.fname(scode) # aggregated sj file
        args = [(sjpaths, asjpath, x) for x in chroms]
        UT.makedirs(os.path.dirname(asjpath))
        rslts = UT.process_mp(make_sj_bed_chr, args, np, doreduce=False)

        # concatenate
        LOG.debug('merge sj: concatenating chroms...')
        UT.makedirs(os.path.dirname(asjpath))
        with open(asjpath,'wb') as dst:
            for x in chroms:
                sf = fn.fname('{0}{1}.gz'.format(scode,x))
                with open(sf,'rb') as src:
                    dst.write(src.read())

        dstpath = fn.sj0_bed() #fn.fname('sj0.bed.gz', category='output') # before selection

        # group same junctions
        msj = UT.read_pandas(asjpath, names=['chr','st','ed','strand','src','ucnt','mcnt'])
        # average <= 2016-04-28 don't average just aggregate
        # scale = 1/float(len(sjpaths)) 
        # msj['ucnt'] = scale*msj['ucnt']
        # msj['mcnt'] = scale*msj['mcnt']
        # unique junctions
        msjg = msj.groupby(['chr','st','ed','strand'])[['ucnt','mcnt']].sum().reset_index()
        # msjg['sc1'] = msjg['ucnt'] # for BED
        # msjg['tst'] = msjg['mcnt'] # for BED
        # u = msjg['ucnt'].map('{:.2}'.format)
        # m = msjg['mcnt'].map('{:.2}'.format)
        u = msjg['ucnt'].astype(str)
        m = msjg['mcnt'].astype(str)
        msjg['_id'] = N.arange(len(msjg))
        msjg['name'] = msjg['_id'].astype(str)+'_u:'+u+'_m:'+m
        cols = GGB.SJCOLS #GGB.BEDCOLS[:7] # chr,st,ed,name,sc1,strand,tst
        UT.write_pandas(msjg[cols], dstpath, '') # BED file
        self.sj0 = msjg

    def collect_sj(self):
        """From aggregated junctions, collect original reads for each samples.

        Inputs:
            Aggregated junction file ('sj0.bed.gz')
            Sample junction files (input to original assemblies ~ SJ.out.tab)

        Outputs:
            'allsj.txt.gz'
        """
        # [Q] faster if make this into chr-wise?
        fn = self.fnobj
        pr = self.params

        if hasattr(self, 'sj0'):
            msj = self.sj0.copy()
        else:
            msj = GGB.read_sj(fn.sj0_bed())
        # sc1: ucnt, tst: mcnt

        #sjpaths = fn.sjopaths() # [(name,sjpath),...], output of assembly (restricted)
        sjpaths = fn.sjpaths() # [(name,sjpath),...], input of assembly (all observed junctions)
        snames = [x[0] for x in sjpaths]
        msj['locus'] = UT.calc_locus_strand(msj)
        msjname = UT.write_pandas(msj[['locus']], fn.txtname('msj'))
        np = pr['np']
        n = int(N.ceil(len(sjpaths)/np))
        args = []
        files = []
        for i in range(np):
            sjpathpart = sjpaths[i*n:(i+1)*n]
            allsjpartname =  fn.txtname('allsjpart.{0}'.format(i))
            statpartname = fn.txtname('allsjstats.{0}'.format(i))
            args.append((sjpathpart, msjname, allsjpartname, statpartname, i))
            files.append((allsjpartname, statpartname))

        rslts = UT.process_mp(collect_sj_part, args, np, doreduce=False)

        # concat allsj, retrieve stats
        dets = []
        maxs = []
        mohs = []
        tots = []
        with open(fn.allsj_txt(), 'wb') as dst:
            for aspn, spn in files:
                with open(aspn, 'rb') as src:
                    shutil.copyfileobj(src, dst) # allsj transposed (row=samples)
                df = UT.read_pandas(spn,index_col=[0])
                dets.append(df['#detected'])
                maxs.append(df['maxcnt'])
                mohs.append(df['maxoverhang'])
                tots.append(df['totcnt'])

        dfdet = PD.concat(dets, axis=1)
        dfmax = PD.concat(maxs, axis=1)
        dfmoh = PD.concat(mohs, axis=1)
        dftot = PD.concat(tots, axis=1)

        l2d = UT.series2dict(dfdet.sum(axis=1))
        l2m = UT.series2dict(dfmax.max(axis=1))
        l2o = UT.series2dict(dfmoh.max(axis=1))
        l2t = UT.series2dict(dftot.sum(axis=1))

        msj['#detected'] = [l2d[x] for x in msj['locus']]
        msj['maxcnt'] = [l2m[x] for x in msj['locus']]
        msj['maxoverhang'] = [l2o[x] for x in msj['locus']]
        msj['totcnt'] = [l2t[x] for x in msj['locus']]

        cols = ['locus','#detected','maxcnt','totcnt','maxoverhang']
        UT.write_pandas(msj[cols], fn.allsj_stats(), 'h')
        self.allsj = msj[cols].copy()
        UT.transpose_csv(fn.allsj_txt(), fn.allsjT_txt())
        

    def select_sj(self):
        pr = self.params
        fn = self.fnobj
        np = pr['np']
        chroms = self.chroms

        # start from sj0
        if hasattr(self, 'sj0'):
            sj0 = self.sj0
        else:
            self.sj0 = sj0 = GGB.read_sj(fn.sj0_bed())
        # threshold #detected, maxcnt
        if hasattr(self, 'allsj'):
            allsj = self.allsj
        else:
            self.allsj = allsj = UT.read_pandas(fn.allsj_stats())
        l2d = UT.df2dict(allsj, 'locus', '#detected')
        l2m = UT.df2dict(allsj, 'locus', 'maxcnt')
        l2o = UT.df2dict(allsj, 'locus', 'maxoverhang')
        sj0['locus'] = UT.calc_locus_strand(sj0)
        sj0['str_id'] = UT.calc_locus(sj0)
        sj0['#detected'] = [l2d[x] for x in sj0['locus']]
        sj0['maxcnt'] = [l2m[x] for x in sj0['locus']]
        sj0['maxoverhang'] = [l2o[x] for x in sj0['locus']]

        idx1 = (sj0['ucnt']>pr['uth'])|(sj0['mcnt']>pr['mth']) 
        idx2 = sj0['#detected']>pr['th_detected']
        yo = sj0['maxcnt']
        xo = sj0['#detected']
        i0 = pr['i_maxcnt']
        i1 = pr['i_detected']
        if i1<1:
            LOG.warning('i_detected should be >0, setting to 1')
            pr['i_detected'] = 1
            i1 = 1
        slope = -(float(i0)/i1)
        idx3 = (yo - (i0 + slope*xo))>0 # above the corner
        idx4 = sj0['maxoverhang']>pr['th_maxoverhang']
        idx5 = sj0['maxcnt']>pr['th_maxcnt']

        # when there's ucnt (=sum of all uniq reads) that should be larger than maxcnt 
        # if not there is possibly repeats associated bad mapping
        # idx5 = ~((sj0['ucnt']>0)&(sj0['ucnt']<sj0['maxcnt'])) # ~12K 
        # this was a bad idea, does not work at all

        sj0['th_uthmth'] = idx1.astype(int)
        sj0['th_detected'] = idx2.astype(int)
        sj0['th_corner'] = idx3.astype(int)
        sj0['th_overhang'] = idx4.astype(int)
        sj0['th_maxcnt'] = idx5.astype(int)
        UT.write_pandas(sj0, fn.txtname('sj0.with.thresholds',category='stats'))

        sj1 = sj0[idx1&idx2&idx3&idx4&idx5].copy()

        UT.write_pandas(sj1, fn.sj1_txt())

        LOG.info('selectsj: {0}/{1} uth,mth)'.format(N.sum(idx1), len(sj0)))
        LOG.info('selectsj: remove {0} with th_detected({1})'.format(N.sum((~idx2)),pr['th_detected']))
        LOG.info('selectsj: remove {0} with corner, {1},{2})'.format(N.sum((~idx3)),i0,i1))
        LOG.info('selectsj: remove {0} with overhang {1})'.format(N.sum(~idx4),pr['th_maxoverhang']))
        LOG.info('selectsj: remove {0} with th_maxcnt {1})'.format(N.sum(~idx5),pr['th_maxcnt']))
        # LOG.info('selectsj: remove {0} ucnt<maxcnt)'.format(N.sum(~idx5)))
        LOG.info('selectsj: sj0:{0}=>sj1:{1}'.format(len(sj0), len(sj1)))
        self.stats['SELECTSJ.th_detected'] = N.sum(idx2)
        self.stats['SELECTSJ.corner'] = N.sum(idx3)
        self.stats['SELECTSJ.uthmth'] = N.sum(idx1)
        self.stats['SELECTSJ.overhang'] = N.sum(idx4)
        self.stats['SELECTSJ.th_maxcnt'] = N.sum(idx5)
        # self.stats['SELECTSJ.ucnt_maxcnt'] = N.sum(idx5)
        self.stats['SELECTSJ.#sj0'] = len(sj0)
        self.stats['SELECTSJ.#sj1'] = len(sj1)

        # CALCULATE ratio and filter  
        args = []
        sj2files = []
        cols0 = GGB.SJCOLS
        for chrom in self.chroms:
            # make sj1chroms with 
            tmp = sj1[sj1['chr']==chrom]
            sj1chrompath = fn.bedname('sj1.{0}'.format(chrom))
            UT.write_pandas(tmp[cols0], sj1chrompath, '')
            ovlchrompath = fn.fname('sj1.ovl.{0}.txt'.format(chrom))
            # sj4chrompath = fn.txtname('sj4.{0}'.format(chrom))
            sj2chrompath = fn.txtname('sj2.{0}'.format(chrom))
            sj2files.append(sj2chrompath)
            # args.append((sj1chrompath, ovlchrompath, sj4chrompath, sj2chrompath, pr['th_ratio'], chrom))
            # th_ratio=1e-2, # discard junctions less than this portion within overlapping junctions
            args.append((sj1chrompath, ovlchrompath, sj2chrompath, pr['th_ratio'], chrom))
        
        # select_sj_chr(sj0chrompath, ovlpath, sj4chrompath, sj2chrompath, th_ratio)
        rslts = UT.process_mp(select_sj_chr, args, np, doreduce=False)
        
        # rslts contains [(chrom, [locus,...], stats),...]
        # rslts contains [(chrom, [str_id,...], stats),...]  # no strand 2016-05-10
        sel = []
        for chrom, loci, stats in rslts:
            sel += loci
            self.stats.update(stats)
        # self.sj5 = sj5 = sj1.set_index('locus').ix[sel]
        self.sj5 = sj5 = sj1.set_index('str_id').ix[sel]
        UT.write_pandas(sj5, fn.sj5_txt())
        self.stats['#sj5'] = len(sj5)

        # concatenate sj2
        sj2path = fn.txtname('sj2',category='output')
        with open(sj2path, 'wb') as dst:
            for f in sj2files:
                with open(f, 'rb') as src:
                    shutil.copyfileobj(src, dst)
        for f in sj2files:
            os.unlink(f)

    def remove_jie(self):
        if hasattr(self, 'sj5'):
            sj = self.sj5
        else:
            self.sj5 = sj = UT.read_pandas(fn.sj5_txt())

        fn = self.fnobj
        pr = self.params
        stats = self.stats
        sjcols = ['chr','st','ed','name','ucnt','strand','mcnt'] # has to match GGB.BEDCOLS[:7]
        sjfile = UT.write_pandas(sj[sjcols], fn.bedname('sj5'), '') # BED used in BT.calc_ovlratio
        sj['str_id'] = UT.calc_locus(sj)

        def _get_idx(which):
            bwfile = fn.ex_bw(which)
            acov = BW.get_totbp_covbp_bw(bwfile, pr['genome'], ['chr1']).ix['acov'].values[0]
            LOG.info('REMOVE_JIE ({0}) acov={1}'.format(which, acov))
            jie_binth = pr['jie_binth']*acov
            jie_sjth = pr['jie_sjth']*acov # seems no need to scale sjth ?
            stats['REMOVEJIE({0}).jie_binth'.format(which)] = jie_binth
            stats['REMOVEJIE({0}).jie_sjth'.format(which)] = jie_sjth
            stats['REMOVEJIE({0}).acov'.format(which)] = acov
            # covarage file
            bedfile = fn.bedname2('bw'+which,jie_binth)
            binfile = BW.bw2bed(
                bwfile=bwfile,
                bedfile=bedfile,
                chroms=UT.chroms(pr['genome']),
                th=jie_binth
            )
            jiebw = GGB.read_bed(binfile)
            tname = fn.txtname('removejie.bw.ovl.'+which)
            sjmp = BT.calc_ovlratio(
                aname=sjfile, 
                bname=binfile, 
                tname=tname, 
                nacol=7, 
                nbcol=3, 
                idcol=['chr','st','ed','strand']
            )
            # match records between sjmp and mg.sj
            sjmp['str_id'] = UT.calc_locus(sjmp)
            sid2ovl = UT.df2dict(sjmp, 'str_id','ovlratio')
            ovlcol = 'ovlratio.'+which
            sj[ovlcol] = [sid2ovl.get(x,N.nan) for x in sj['str_id']]
            # should use count ratios instead of actual reads as threshold ?
            th = jie_sjth
            idx = (sj[ovlcol]>=pr['jie_ovlth'])&(sj['ucnt']<th)&(sj['mcnt']<th)
            return idx

        self.idxp = idxp = _get_idx('mep')
        self.idxn = idxn = _get_idx('men')
        LOG.debug('REMOVEJIE.idxp hits = {0}'.format(N.sum(idxp)))
        LOG.debug('REMOVEJIE.idxn hits = {0}'.format(N.sum(idxn)))
        jieidx = idxp|idxn
        LOG.debug('REMOVEJIE.jieidxn hits = {0}'.format(N.sum(jieidx)))

        sj1 = sj[~jieidx].copy() # use these for "nearest donor/acceptor" exon extraction
        jie = sj[jieidx].copy() # junctions in exon, add later
        self.info = '#sj:{0}=>{1}, jie {2}'.format(len(sj), len(sj1), len(jie))
        stats['REMOVEJIE.#sj'] = len(sj1)
        stats['REMOVEJIE.#jie'] = len(jie)
        #return sj1, jie
        self.sj6 = sj1
        self.jie = jie
        UT.write_pandas(self.sj6, fn.sj6_txt())
        
    def remove_jie2(self):
        # adaptive threshold version
        if hasattr(self, 'sj5'):
            sj = self.sj5
        else:
            self.sj5 = sj = UT.read_pandas(fn.sj5_txt())

        fn = self.fnobj
        pr = self.params
        stats = self.stats
        sjcols = ['chr','st','ed','name','ucnt','strand','mcnt'] # has to match GGB.BEDCOLS[:7]
        sjfile = UT.write_pandas(sj[sjcols], fn.bedname('sj5'), '') # BED used in BT.calc_ovlratio
        sj['str_id'] = UT.calc_locus(sj)

        def _get_idx(which):
            bwfile = fn.ex_bw(which)
            acov = BW.get_totbp_covbp_bw(bwfile, pr['genome'], ['chr1']).ix['acov'].values[0]
            LOG.info('REMOVE_JIE ({0}) acov={1}'.format(which, acov))

            # find high cov regions
            jie_binth = pr['jie_acovfactor']*acov
            bedfile = fn.bedname2('bw'+which,jie_binth)
            binfile = BW.bw2bed(
                bwfile=bwfile,
                bedfile=bedfile,
                chroms=UT.chroms(pr['genome']),
                th=jie_binth
            )
            # find overlap of junctions and high cov region
            jiebw = GGB.read_bed(binfile)

            tname = fn.txtname('removejie.bw.ovl.'+which)
            sjmp = BT.calc_ovlratio(
                aname=sjfile, 
                bname=binfile, 
                tname=tname, 
                nacol=7, 
                nbcol=3, 
                idcol=['chr','st','ed'],
                returnbcols=True
            )
            LOG.debug(sjmp.columns)
            # match records between sjmp and mg.sj
            sjmp['str_id'] = UT.calc_locus(sjmp) # sjmp [...](7 sj cols)+[b_chr,b_st,b_ed]
            sjmp['hcid'] = UT.calc_locus(sjmp, 'b_chr', 'b_st', 'b_ed')
            # calc high cov region coverages
            hcov = CC.calc_cov_mp(
                bed=jiebw, 
                bwname=bwfile,
                fname=fn.txtname('removejie.bw.cov.'+which),
                np=pr['np'], 
                which='cov')
            hcov['hcid'] = UT.calc_locus(hcov)
            hcid2cov = UT.df2dict(hcov, 'hcid', 'cov')
            sjmp['hccov'] = [hcid2cov.get(x,N.nan) for x in sjmp['hcid']]
            sjmpovl = sjmp[sjmp['ovlratio']>=pr['jie_ovlth']] # target junctions
            sid2cov = UT.df2dict(sjmpovl, 'str_id', 'hccov') # there should be only one overlapping hc interval
            thcol = 'covth.'+which
            sj[thcol] = [sid2cov.get(x,0) for x in sj['str_id']]
            sj[thcol] = sj[thcol]*pr['jie_sjfactor']
            idx = (sj['ucnt']<sj[thcol])&(sj['mcnt']<sj[thcol])
            # idx = (sj['ucnt']<sj[thcol]) # [TODO] it probably makes more sense to just use ucnt

            stats['REMOVEJIE({0}).jie_binth'.format(which)] = jie_binth
            stats['REMOVEJIE({0}).acov'.format(which)] = acov
            stats['REMOVEJIE({0}).removed'.format(which)] = N.sum(idx)
            return idx

        self.idxp = idxp = _get_idx('mep')
        self.idxn = idxn = _get_idx('men')
        LOG.debug('REMOVEJIE.idxp hits = {0}'.format(N.sum(idxp)))
        LOG.debug('REMOVEJIE.idxn hits = {0}'.format(N.sum(idxn)))
        jieidx = idxp|idxn
        LOG.debug('REMOVEJIE.jieidxn hits = {0}'.format(N.sum(jieidx)))

        sj1 = sj[~jieidx].copy() # use these for "nearest donor/acceptor" exon extraction
        jie = sj[jieidx].copy() # junctions in exon, add later
        self.info = '#sj:{0}=>{1}, jie {2}'.format(len(sj), len(sj1), len(jie))
        stats['REMOVEJIE.#sj'] = len(sj1)
        stats['REMOVEJIE.#jie'] = len(jie)
        #return sj1, jie
        self.sj6 = sj1
        self.jie = jie
        UT.write_pandas(self.sj6, fn.sj6_txt())

    def write_sjpn(self):
        fn = self.fnobj
        if hasattr(self, 'sj6'):
            sj6 = self.sj6
        else:
            sj6 = UT.read_pandas(fn.sj6_txt())
        cols = GGB.SJCOLS
        sj6p = sj6[sj6['strand'].isin(['+','.'])][cols]
        sj6n = sj6[sj6['strand'].isin(['-','.'])][cols]
        UT.write_pandas(sj6p, fn.sj_bed('p'), '')
        UT.write_pandas(sj6n, fn.sj_bed('n'), '')
        self.stats['#sj6.p'] = len(sj6p)
        self.stats['#sj6.n'] = len(sj6n)

    def aggregate_bigwigs0(self):
        fn = self.fnobj
        pr = self.params
        bwfiles = [x[1] for x in fn.bwpaths()] # [(name,bwfile),...]
        dstpath = fn.agg_bw()
        # scale = 1./len(bwfiles) # average
        # BW.merge_bigwigs_mp(bwfiles, pr['genome'], dstpath, scale=scale, np=pr['np'])
        BW.merge_bigwigs_mp(bwfiles, pr['genome'], dstpath, scale=None, np=pr['np'])

    def aggregate_bigwigs(self):
        fn = self.fnobj
        pr = self.params

        # all
        LOG.info('collecting *.all.bw multimapper weighted')
        bwfiles = [x[1]+'.all.bw' for x in fn.bwpres()] # [(name,bwfile),...]
        dstpath = fn.agg_bw()
        BW.merge_bigwigs_mp(bwfiles, pr['genome'], dstpath, scale=None, np=pr['np'])
        LOG.info('wrote to {0}'.format(dstpath))

        # unique
        LOG.info('collecting *.uniq.bw only unique reads')
        bwfiles = [x[1]+'.uniq.bw' for x in fn.bwpres()] # [(name,bwfile),...]
        dstpath = fn.agguniq_bw()
        BW.merge_bigwigs_mp(bwfiles, pr['genome'], dstpath, scale=None, np=pr['np'])
        LOG.info('wrote to {0}'.format(dstpath))


def make_ex_bed_chr(expaths, dstpre, chrom, covth, covdelta, tgts, minsecovth, secovfactor):
    withcov = True
    paths = {k:dstpre+chrom+k for k in tgts}
    dst = {k:open(paths[k],'w') for k in tgts }
    for i,(name, path) in enumerate(expaths):
        ex = UT.read_pandas(path)
        if withcov:
            ex['dup'] = ((ex['cov']-covth)/covdelta).astype(int)+1
        ex = ex[(ex['chr']==chrom)&(ex['cov']>covth)]
        me = ex[ex['cat']!='s']
        se = ex[ex['cat']=='s'].copy()
        comb = {'mep':(me,('+','.')),
                'men':(me,('-','.')),
                'sep':(se,('+','.')),
                'sen':(se,('-','.')),
                'se':(se,['.'])}
        comb = {t:comb[t] for t in tgts}
        for k,(tgt,strand) in comb.items():
            tgt = tgt[tgt['strand'].isin(strand)]
            if k[:2]=='se': # for SE only use high confidence ones
                # get secovth from stats.txt
                stpath = path.replace('.ex.txt.gz','.assemble.stats.txt')
                if os.path.exists(stpath):
                    stdf = UT.read_pandas(stpath, index_col=[0])
                    scth = max(minsecovth, secovfactor*stdf.ix['FINDSECOVTH.secovth']['value'])
                else:
                    scth = minsecovth
                # threshold
                idx = tgt['cov']>scth
                n0 = len(tgt)
                n1 = N.sum(idx)
                if n0>0:
                    LOG.debug('{4}:{5}({0}).scth={1}, {2}=>{3}'.format(chrom,scth,n0,n1,k,name))
                tgt = tgt[idx]
            if withcov:
                def _gen():
                    for chrom,st,ed,dup in UT.izipcols(tgt, ['chr','st','ed','dup']):
                        for i in range(dup):
                            yield '{0}\t{1}\t{2}\n'.format(chrom,st,ed)
                recs = [x for x in _gen()]
                txt = ''.join(recs)
            else:
                if k=='se':# save srcname, cov
                    #tgt['sname'] = path.split('/')[-1].replace('.gz','').replace('.txt','').replace('.ex2','').replace('.ex','')
                    tgt['sname'] = name
                    tmp = tgt[['chr','st','ed','sname','cov']].apply(lambda x: '\t'.join(map(str, x)),axis=1)
                    txt = '\n'.join(tmp.values)+'\n'
                else:   
                    txt = '\n'.join((tgt['chr']+'\t'+tgt['st'].astype(str)+'\t'+tgt['ed'].astype(str)).values)+'\n'
            dst[k].write(txt)
    for k, v in dst.items():
        v.close()
        UT.compress(paths[k])

def make_sj_bed_chr(sjpaths,dstpath,chrom):
    cols = ['chr','st','ed','strand','src','ucnt','mcnt']
    wpath = dstpath+chrom
    # n = len(sjpaths) # how many files?
    # scale = 1/float(n)
    with open(wpath,'w') as dst:
        for i, (name,spath) in enumerate(sjpaths):
            sj = GGB.read_sj(spath)
            #scale = 1e6/float(aligned)
            sj['_id'] = N.arange(len(sj))
            #name = os.path.basename(spath)[:-len('sj.txt.gz')]
            sj['src'] = name+':'+sj['_id'].astype(str)
            #sj['ucnt'] = sj['ucnt']*scale # average over all samples
            #sj['mcnt'] = sj['mcnt']*scale
            # sj0 = sj[(sj['chr']==chrom)&((sj['ucnt']>=uth)|(sj['mcnt']>=mth))][cols]
            # at this stage collect everything 
            sj0 = sj[(sj['chr']==chrom)][cols]
            txt = '\n'.join(['\t'.join(map(str, x)) for x in sj0.values])
            dst.write(txt+'\n')
    return UT.compress(wpath)

def select_sj_chr(sj1chrompath, ovlpath, sj2chrompath, th_ratio, chrom):
    """Select aggregated junctions according to several metrics"""
    # calc self intersection to find overlapping junctions
    a = b = sj1chrompath
    c = ovlpath
    c = BT.bedtoolintersect(a,b,c,wao=True)
    # calc ratio
    cols0 = GGB.SJCOLS
    cols = cols0+['b_'+x for x in cols0]+['ovl']
    sjovl = UT.read_pandas(c, names=cols)

    # [TODO] select overlaps > some threshold (>50%)
    # sjovl = sjovl[sjovl['strand']==sjovl['b_strand']] # same strand
    LOG.debug('select_sj_chr:{1}:len(sjovl)={0}'.format(len(sjovl),chrom))

    # sjgr = sjovl.groupby(['chr','st','ed','strand']) 
    sjgr = sjovl.groupby(['chr','st','ed'])  # ignore strand
    sj2 = sjgr[['ucnt','mcnt','name']].first()
    sj2['ucnt_sum'] = sjgr['b_ucnt'].sum()
    sj2['mcnt_sum'] = sjgr['b_mcnt'].sum()
    sj2['sum'] = sj2['ucnt_sum']+sj2['mcnt_sum']
    sj2['cnt'] = sj2['ucnt']+sj2['mcnt']
    # self.sj2 = sj2 = sj2.reset_index() # need chr,st,ed,strand at next step
    sj2 = sj2.reset_index() # need chr,st,ed,strand at next step
    # sj2['locus'] = UT.calc_locus_strand(sj2)
    sj2['str_id'] = UT.calc_locus(sj2)
    sj2['ratio'] = sj2['ucnt']/sj2['ucnt_sum']
    sj2['ratio_m'] = sj2['mcnt']/sj2['mcnt_sum']
    sj2['ratio_a'] = sj2['cnt']/sj2['sum']

    # select 
    # th_ratio=1e-2, # discard junctions less than this portion within overlapping junctions    
    idx1 = (sj2['ratio']>=th_ratio)|(sj2['ratio_a']>=th_ratio)
    # self.sj4 = sj4 = sj2[idx1&((idx2&idx3)|idx4)]
    sj4 = sj2[idx1]
    # LOG.info('selectsj: in {0}'.format(len(sj2)))
    fname = os.path.basename(sj1chrompath)
    LOG.info('selectsj:{0}<th_ratio({1})(in {3})file:{2}'.format(N.sum(~idx1),th_ratio,fname,len(sj2)))
    cols = GGB.SJCOLS
    # write out selected locus
    #UT.write_pandas(sj4[['locus']], sj4chrompath, 'h')
    # write out sj2 for debug
    UT.write_pandas(sj2, sj2chrompath, 'h')
    stats = {chrom+'.#sj2':len(sj2), chrom+'.#sj4':len(sj4)}
    # return chrom, list(sj4['locus'].values), stats
    return chrom, list(sj4['str_id'].values), stats


def collect_sj_part(sjpathpart, msjname, allsjpartname, statpartname, i):
    msj = UT.read_pandas(msjname) # column locus
    msj1 = msj.copy() 
    maxoh = msj.copy() # max (maxoverhang)
    maxoh['oh'] = 0
    snames = []
    for i, (sname,spath) in enumerate(sjpathpart):
        sj = GGB.read_sj(spath, parsename=True)
        sj['locus'] = UT.calc_locus_strand(sj)
        # sj['cnt'] = (sj['ucnt']+sj['mcnt']) #*scale <== sjbed is already normalized
        sj['jcnt'] = [x or y for x,y in sj[['ucnt','mcnt']].values]
        l2u = UT.df2dict(sj, 'locus', 'jcnt')
        l2o = UT.df2dict(sj, 'locus', 'maxoverhang')
        msj[sname] = [l2u.get(x,0) for x in msj['locus']]
        maxoh['oh'] = [max(y, l2o.get(x,0)) for x,y in maxoh[['locus','oh']].values]
        snames.append(sname)

    msj1['#detected'] = (msj[snames]>0).sum(axis=1) # number of samples with reads>0
    msj1['maxcnt'] = msj[snames].max(axis=1) # max reads
    msj1['totcnt'] = msj[snames].sum(axis=1)
    msj1['maxoverhang'] = maxoh['oh']

    if i==0: # first one writes header
        cols = ['locus']+snames
        UT.write_pandas(msj[cols].T, allsjpartname, 'ih')
    else:
        UT.write_pandas(msj[snames].T, allsjpartname, 'i')
    UT.write_pandas(msj1[['locus','#detected','maxcnt','maxoverhang','totcnt']], statpartname, 'h')

    return (allsjpartname, statpartname)

        

class MergeAssemble(object):
    """Merge multiple assemblies into one. 

    1. run assembler for each strand
    2. combine outputs from each strand (ME: multi-exons)
    3. detect SE (single-exons)
    4. combine ME and SE
    5. calculate coverage based on averaged bigwig
    6. calculate junction coverage based on aggregated junctions

    """

    ecols = ['chr','st','ed','name','sc1','strand',
             '_id','_gidx','gname','cat','ptyp','len',
             'a_id','d_id','a_degree','d_degree','a_pos','d_pos']
    scols = ['chr','st','ed','name','sc1','strand',
             '_id','_gidx','gname','st-1',
             'a_id','d_id','a_degree','d_degree','a_pos','d_pos',
            ]

    def __init__(self, fni, fna, datacode='', saveintermediates=False, **kw):
        """
        Args:
            fni: MergeInputNames object
            fna: MergeAssemblyNames object

        Keywords:
            can be used to modify assembly parameters

        """
        self.fni = fni
        self.fna = fna
        self.params = AS.MPARAMS.copy()
        self.params.update(MERGEASMPARAM)
        self.params.update(kw)
        self.stats = {}
        self.fnobj = fna
        self.datacode = datacode
        self.kw = kw
        self.saveintermediates = saveintermediates
        # set following params according to number of samples
        # jie_binth
        # jie_ratio
        # jie_sjth
        # se_binth

        self.make_assemblers()

    def make_assemblers(self):
        fni = self.fni
        fna = self.fna
        pr = self.params
        sjexdic = {'mep': {'bw':fni.ex_bw('mep'),
                           'sj':fni.sj_bed('p')},
                   'men': {'bw':fni.ex_bw('men'),
                           'sj':fni.sj_bed('n')}}
        # FileNames objects for assemblers
        fns = {k:FN.FileNames(sname = '{0}.{1}'.format(fna.code,k),
                              bwfile = sjexdic[k]['bw'],
                              sjfile = sjexdic[k]['sj'],
                              outdir = fna.outdir,
                              refgtf = fna.refgtf.gtfpath) for k in sjexdic}
        savei = self.saveintermediates
        self.asms = asms = {k: AS.Assembler(fns[k], saveintermediates=savei, **pr) for k in fns}
        asms['men'].params['binstrand']='-'
        asms['mep'].params['binstrand']='+'


    def assemble(self):
        self.assemble_me1()
        self.assemble_me2()
        # self.assemble_se() # old method (threshold by max)
        self.assemble_se2() # power law selection + high confidence from individual
        # self.assemble_se3() # use high conf from each sample just threshold at acov/10
        self.assemble_combine()
        self.assemble_writefiles()
        self.calc_merged_covs()
        self.assign_sjcnt()
        self.make_unionex()
        if not self.saveintermediates:
            self.fna.delete(delete=[],protect=['output'])
            # also delete outputs of mep, men assemblies
            self.asms['mep'].fnobj.delete(['output'],['stats'])
            self.asms['men'].fnobj.delete(['output'],['stats'])

    def assemble_me1(self):
        """do assembly separately for each strand"""
        asms = self.asms
        LOG.info('#########  START +strand assembly ######################################')
        asms['mep'].assemble()
        LOG.info('#########  START -strand assembly ######################################')
        asms['men'].assemble()        
        LOG.info('########################################################################')
        LOG.info('mep:{0}, men:{1}'.format(len(asms['mep'].ae),len(asms['men'].ae)))

    def _remove_se_from_me(self):
        fna = self.fna
        fnp = self.asms['mep'].fnobj
        fnn = self.asms['men'].fnobj
        exp = UT.read_pandas(fnp.ex_out())
        exn = UT.read_pandas(fnn.ex_out())
        mep,sep = UT.mese(exp)
        men,sen = UT.mese(exn)
        # FIX _gidx! 2016-03-22 
        #exp['_gidx'] = N.abs(exp['_gidx'])
        #exn['_gidx'] = -N.abs(exn['_gidx'])
        # ====> just remove all SE from this stage
        # remove SE overlapping opposite strand
        # def _select(se1,me2):
        #     cols0 = ['chr','st','ed','name','_id','strand']
        #     a = fna.fname('setmp.bed.gz')
        #     b = fna.fname('metmp.bed.gz')
        #     c = fna.fname('semetmp.bed.gz')
        #     a = UT.write_pandas(se1[cols0], a, '')
        #     b = UT.write_pandas(me2[cols0], b, '')
        #     c = PO.BT.bedtoolintersect(a,b,c,wao=True) # get original entry
        #     cols = cols0+['b_'+x for x in cols0]+['ovl']
        #     cdf = UT.read_pandas(c,names=cols)
        #     cdfg = cdf.groupby('_id')
        #     ovl = cdfg['ovl'].sum()
        #     #siz = cdfg.size()
        #     #nonovl_se = ovl[(ovl==0)|(siz>1)].index.values 
        #     nonovl_se = ovl[ovl==0].index.values 
        #     se2 = se1.set_index('_id').ix[nonovl_se].reset_index()
        #     return se2
        #sep2 = _select(sep,men)
        #sen2 = _select(sen,mep)
        #exp2 = PD.concat([mep,sep2],ignore_index=True)
        #exn2 = PD.concat([men,sen2],ignore_index=True)
        #return exp2,exn2
        return mep, men

    def assemble_me2(self):
        """combine strands for ME"""
        fna = self.fna
        fnp = self.asms['mep'].fnobj
        fnn = self.asms['men'].fnobj

        exp,exn = self._remove_se_from_me()
        sjp = UT.read_pandas(fnp.sj_out())
        sjn = UT.read_pandas(fnn.sj_out())
        # keep a/d id but assign sign
        for c in ['a_id', 'd_id']:
            exn[c] = -exn[c]
            sjn[c] = -sjn[c]
        self.expn = expn = PD.concat([exp,exn], ignore_index=True)
        self.sjpn = sjpn = PD.concat([sjp,sjn], ignore_index=True)
        # renew unique id 
        expn['_id'] = N.arange(len(expn))
        sjpn['_id'] = N.arange(len(sjpn))

        # stats
        n0 = len(set(expn['_gidx'].values))
        np = len(set(exp['_gidx'].values))
        nn = len(set(exn['_gidx'].values))
        LOG.info('n0:{0}, np:{1}, nn:{2}, np+nn:{3}'.format(n0,np,nn,np+nn))
        
        # write EX,SJ
        expn['len'] = expn['ed']-expn['st']
        UT.write_pandas(expn[self.ecols], fna.fname('mepn.ex.txt.gz'), 'h')
        UT.write_pandas(sjpn[self.scols], fna.fname('mepn.sj.txt.gz'), 'h')
        
        # GENES BED
        gp = GGB.read_bed(fnp.genes_out())
        gn = GGB.read_bed(fnn.genes_out())
        gidp = set(exp['gname'].values)
        gidn = set(exn['gname'].values)
        gp = gp[[x in gidp for x in gp['name']]]
        gn = gn[[x in gidn for x in gn['name']]]
        self.genes = genes = PD.concat([gp,gn],ignore_index=True)
        GGB.write_bed(genes, fna.fname('mepn.genes.bed.gz'), ncols=12)

    def assemble_se(self):
        """ Calculate SE candidate (subtract ME) 
        Currently only filter with maxcov.

        This part needs to be improved.

        """
        # [TODO] also process sep, sen (stranded SEs)
        # [TODO] do power law fitting and adaptively find secov threshold
        # [Q] does power law still apply for aggregated coverages?

        fna = self.fna
        fni = self.fni
        pr = self.params

        if hasattr(self, 'expn'):
            expn = self.expn
        else:
            self.expn = expn = UT.read_pandas(fna.fname('mepn.ex.txt.gz'))
            self.sjpn = sjpn = UT.read_pandas(fna.fname('mepn.sj.txt.gz'))

        sebin = BW.bw2bed_mp(
                    bwfile=fni.ex_bw('se'), 
                    bedfile=fna.fname('sebw0.bed.gz'), 
                    chroms=UT.chroms(pr['genome']), 
                    th=0,
                    np=pr['np']
                    )

        mefile = GGB.write_bed(expn, fna.fname('mepn.me.bed.gz'), ncols=3)
        sufile = BT.bedtoolintersect(sebin,mefile,fna.fname('mepn.se-me.bed.gz'),v=True) # -v subtract
        df = GGB.read_bed(sufile)

        # calculate SECOV, SEMAX
        self.secov = secov = CC.calc_cov_mp(
                                    bed=df, 
                                    bwname=fni.ex_bw('se'), 
                                    fname=fna.fname('secov.txt.gz'), 
                                    np=pr['np'], 
                                    which='cov')
        self.semax = semax = CC.calc_cov_mp(
                                    bed=df, 
                                    bwname=fni.ex_bw('se'), 
                                    fname=fna.fname('semax.txt.gz'), 
                                    np=pr['np'], 
                                    which='max')
        semax['cov'] = secov['cov']
        # threshold to get SE
        self.se0 = se0 = semax[semax['max']>pr['se_maxth']].copy()
        
        # save 
        gid0 = max(pr['se_gidstart'], N.max(N.abs(expn['_gidx'])))
        se0['_gidx'] = N.arange(gid0,gid0+len(se0))
        se0['name'] = ['JS{0}'.format(x) for x in se0['_gidx']]
        se0['gname'] = se0['name']
        se0['sc1'] = se0['max']
        se0['strand'] = '.'
        sename = fna.fname('se.bed.gz',category='output')
        GGB.write_bed(se0, sename, ncols=6)

    def assemble_se2(self):
        """ 
        1. Calculate SE candidate using allsample.bw (ME subtracted) 
        2. Apply FINDSECOVTH procedure to find threshold
        3. Optionally addin high confidence SE from each sample (in se.bw)
        """
        # [TODO] also process sep, sen (stranded SEs)
        # [Q] Does power law still apply for aggregated coverages?
        # [A] Using gen4 and allsample.bw, ME follows power law
        #     SE has long (somewhat noisy) tail but has the same slope in the initial half
        #     segment. So, applying power law to find threshold to secov calculated from
        #     allsample.bw probably is OK. 
        #     However, secov calculated from bw from model does not really follow power law.
        #     Likely because model SEs are already thresholded at each sample.

        fna = self.fna
        fni = self.fni
        pr = self.params

        if hasattr(self, 'expn'):
            expn = self.expn
        else:
            self.expn = expn = UT.read_pandas(fna.fname('mepn.ex.txt.gz'))
            self.sjpn = sjpn = UT.read_pandas(fna.fname('mepn.sj.txt.gz'))

        sebin = BW.bw2bed_mp(
                    bwfile=fni.ex_bw('se'),  # fni.agg_bw(), # using allsample.bw is not a good idea makes huge intervals
                    bedfile=fna.fname('sebw.bed.gz'), 
                    chroms=UT.chroms(pr['genome']), 
                    th=pr['se_binth'],  #setting th>0 screws secovth calc? 
                    np=pr['np']
                    )

        mefile = GGB.write_bed(expn, fna.fname('mepn.me.bed.gz'), ncols=3)
        sufile = BT.bedtoolintersect(sebin,mefile,fna.fname('mepn.se-me.bed.gz'),v=True) # -v subtract
        df = GGB.read_bed(sufile)

        # calculate SECOV
        if pr['findsecov_usesemax']:
            self.semax = semax = CC.calc_cov_mp(
                                        bed=df, 
                                        bwname=fni.agg_bw(), #fni.ex_bw('se'), 
                                        fname=fna.txtname('se.max.all'), 
                                        np=pr['np'], 
                                        which='max')

            self.memax = memax = CC.calc_cov_mp(
                                        bed=expn, 
                                        bwname=fni.agg_bw(), #fni.ex_bw('se'), 
                                        fname=fna.txtname('me.max.all'), 
                                        np=pr['np'], 
                                        which='max')
        else:
            self.secov = secov = CC.calc_cov_mp(
                                        bed=df, 
                                        bwname=fni.agg_bw(), #fni.ex_bw('se'), 
                                        fname=fna.txtname('se.cov.all'), 
                                        np=pr['np'], 
                                        which='cov')
            # calculate MECOV
            self.mecov = mecov = CC.calc_cov_ovl_mp(
                                        srcname=expn, 
                                        bwname=fni.agg_bw(), 
                                        dstname=fna.txtname('mepn.cov'), 
                                        np=pr['np'], 
                                        covciname=fna.txtname('mepn.covci'), 
                                        ciname=fna.txtname('mepn.ci'), 
                                        colname='cov', 
                                        override=False)

            # whether to use ecov or ovlcov?
            # self.mecov = meecov = CC.calc_ecov(
            #                             expath=fna.fname('mepn.ex.txt.gz'), 
            #                             cipath=fna.fname('mepn.ci.txt.gz'), 
            #                             bwpath=fni.agg_bw(), 
            #                             dstprefix=fna.fname('mepn'), 
            #                             override=False, 
            #                             np=pr['np'])
            # covciname = fna.fname('mepn.covci.txt.gz') # register as temp files, delete later
            # ecovname = fna.fname('mepn.ecov.txt.gz')

        # * use minsecovth for safeguard
        # * increase secovfactor? 

        # apply powerlaw threshold finding
        self.f = f = AS.FINDSECOVTH(self)
        f.fnobj.sname = fna.code
        if pr['findsecov_usesemax']:
            f.ex = memax
            f.se = semax
            memax['cov'] = memax['max']
            semax['cov'] = semax['max']
            f.find_secovth()
            th = max(f.se_th99, pr['minsecovth'])
            # semax['cov'] = secov['cov'] # secov not calculated
            self.se1 = se1 = semax[semax['max']>th].copy()
        else:
            f.ex = mecov
            f.se = secov
            # secov['max'] = semax['max'] # semax not calculated
            f.find_secovth()
            th = max(f.se_th99, pr['minsecovth'])
            self.se1 = se1 = secov[secov['cov']>th].copy()
        

        self.stats['assemble_se2.secovth_found'] = f.se_th99
        self.stats['assemble_se2.secovth_used'] = th
        self.stats['assemble_se2.#se1'] = len(se1)

        if pr['use_se2']:
            # gather high confidence SE from each sample
            sebin2 = BW.bw2bed_mp(
                        bwfile=fni.ex_bw('se'), 
                        bedfile=fna.fname('sebw0.bed.gz'), 
                        chroms=UT.chroms(pr['genome']), 
                        th=0,
                        np=pr['np']
                        )
            sufile2 = BT.bedtoolintersect(sebin2,mefile,fna.fname('mepn.se2-me.bed.gz'),v=True) 
            # -v subtract (remove overlapped)
            df2 = GGB.read_bed(sufile2)
            self.se2 = se2 = CC.calc_cov_mp(
                                        bed=df2, 
                                        bwname=fni.agg_bw(), #ex_bw('se'), 
                                        fname=fna.fname('se2.cov.all.txt.gz'), 
                                        np=pr['np'], 
                                        which='cov')        
            self.stats['assemble_se2.#se2'] = len(se2)
            LOG.info('#se1={0}, #se2={1}, secovth={2}'.format(len(se1),len(se2),f.se_th99))
            # combine remove overlapped
            cols = ['chr','st','ed','cov']
            s1p = UT.write_pandas(se1[cols], fna.fname('se1.bed.gz'), '')
            s2p = UT.write_pandas(se2[cols], fna.fname('se2.bed.gz'), '')
            sufile3 = BT.bedtoolintersect(s1p,s2p,fna.fname('mepn.se1-se2.bed.gz'),v=True) # -v subtract
            se3 = UT.read_pandas(sufile3, names=cols)
            self.se0 = se0 = PD.concat([se2[cols], se3[cols]], ignore_index=True)
        else:
            LOG.info('#se1={0}, secovth={1}'.format(len(se1),f.se_th99))
            self.se0 = se0 = se1

        # size threshold
        se0['len'] = se0['ed'] - se0['st']
        # self.se0 = se0 = se0[(se0['len']>pr['se_minsize'])&(se0['len']<pr['se_maxsize'])].copy()

        # save 
        gid0 = max(pr['se_gidstart'], N.max(N.abs(expn['_gidx'])))
        se0['_gidx'] = N.arange(gid0,gid0+len(se0))
        se0['name'] = ['JS{0}'.format(x) for x in se0['_gidx']]
        se0['gname'] = se0['name']
        se0['sc1'] = se0['cov']
        se0['strand'] = '.'
        sename = fna.fname('se.bed.gz',category='output')
        GGB.write_bed(se0, sename, ncols=6)

    def assemble_se3(self):
        """ Calculate SE candidate (subtract ME) 
        
        1. set sebin to 1/10 average coverage
        2. take intervals > 100bp        

        """
        fna = self.fna
        fni = self.fni
        pr = self.params

        if hasattr(self, 'expn'):
            expn = self.expn
        else:
            self.expn = expn = UT.read_pandas(fna.fname('mepn.ex.txt.gz'))
            self.sjpn = sjpn = UT.read_pandas(fna.fname('mepn.sj.txt.gz'))

        bwfile = fni.ex_bw('se')
        cdf = BW.get_totbp_covbp_bw(bwfile, pr['genome'])
        binth = cdf.ix['acov'].mean()/pr['sebinth_factor']
        sebin = BW.bw2bed_mp(
                    bwfile=bwfile, 
                    bedfile=fna.fname('sebw0.bed.gz'), 
                    chroms=UT.chroms(pr['genome']), 
                    th=binth,
                    np=pr['np']
                    )

        mefile = GGB.write_bed(expn, fna.fname('mepn.me.bed.gz'), ncols=3)
        sufile = BT.bedtoolintersect(sebin,mefile,fna.fname('mepn.se-me.bed.gz'),v=True) # -v subtract
        se1 = GGB.read_bed(sufile)
        se1['len'] = se1['ed']-se1['st']
        # threshold to get SE
        smin = pr['se_minsize']
        smax = pr['se_maxsize']
        self.se0 = se0 = se1[(se1['len']>smin)&(se1['len']<smax)].copy()
        
        # save 
        gid0 = max(pr['se_gidstart'], N.max(N.abs(expn['_gidx'])))
        se0['_gidx'] = N.arange(gid0,gid0+len(se0))
        se0['name'] = ['JS{0}'.format(x) for x in se0['_gidx']]
        se0['gname'] = se0['name']
        se0['sc1'] = 0 #se0['max']
        se0['strand'] = '.'
        sename = fna.fname('se.bed.gz',category='output')
        GGB.write_bed(se0, sename, ncols=6)

    def assemble_combine(self):
        """Combine ME/SE """
        fna = self.fna
        fni = self.fni
        pr = self.params
        ecols = self.ecols # from assemble_me2
        sjpn = self.sjpn
        expn = self.expn
        if not hasattr(self, 'genes'):
            self.genes = genes = GGB.read_bed(fna.fname('mepn.genes.bed.gz'))
        else:
            genes = self.genes
        se0 = self.se0
        if ('cov' in expn.columns) and ('cov' in se0.columns):
            ecols = ecols+['cov']
        if ('max' in expn.columns) and ('max' in se0.columns):
            ecols = ecols+['max']

        # match SE to ME: ecols
        se0['_id'] = N.arange(len(expn),len(expn)+len(se0))
        se0['cat'] = 's'
        se0['ptyp'] = 's'
        se0['len'] = se0['ed']-se0['st']
        se0['a_id'] = 0
        se0['a_degree'] = 0
        se0['a_pos'] = se0['chr']+':'+se0['st'].astype(str)+':.'
        se0['d_id'] = 0
        se0['d_degree'] = 0
        se0['d_pos'] = se0['chr']+':'+se0['ed'].astype(str)+':.'
        
        self.sj0 = sj0 = sjpn
        self.ex0 = ex0 = PD.concat([expn[ecols],se0[ecols]], ignore_index=True)
        # fix id, ad info
        UT.set_ids(sj0)
        UT.set_ids(ex0)
        #UT.set_ad_info(sj0,ex0)
        
        # make ci
        self.ci0 = ci0 = UT.chopintervals(ex0, fname=fna.ci_out())

        # calculate glen, tlen
        UT.set_glen_tlen(ex0,ci0)
        # tlen = UT.calc_tlen(ex0, ci0)
        # g2tlen = UT.df2dict(tlen, 'index', 'tlen')
        # ex0['tlen'] = [g2tlen[x] for x in ex0['_gidx']]
        # gr = ex0.groupby('_gidx')
        # glen = gr['ed'].max() - gr['st'].min()
        # g2glen = UT.series2dict(glen)
        # ex0['glen'] = [g2glen[x] for x in ex0['_gidx']]

        # adjust genes bed
        se0['tst'] = se0['st']
        se0['ted'] = se0['ed']
        se0['sc2'] = se0['cov'] if ('cov' in se0.columns) else 0
        se0['#exons'] = 1
        se0['esizes'] = se0['len'].astype(str)+','
        se0['estarts'] = '0,'
        bcols = GGB.BEDCOLS
        self.genes0 = genes0 = PD.concat([genes[bcols],se0[bcols]],ignore_index=True)

    def assemble_writefiles(self):
        """ Write EX, SJ, GENES output files """
        fna = self.fna
        scols = self.scols
        GGB.write_bed(self.sj0[scols], fna.sj_out('bed'), ncols=6)        
        UT.write_pandas(self.sj0[scols], fna.sj_out('txt'), 'h')
        
        GGB.write_bed(self.ex0, fna.ex_out('bed'), ncols=6)
        UT.write_pandas(self.ex0, fna.ex_out('txt'), 'h')
        
        GGB.write_bed(self.genes0, fna.genes_out('bed'), ncols=12)
        UT.write_pandas(self.genes0, fna.genes_out('txt'), 'h')

    def calc_merged_covs(self):
        """ calculate ecov, gcov against aggregated bigwig """
        fna = self.fna
        fni = self.fni
        pr = self.params

        expath = fna.ex_out('txt')
        cipath = fna.ci_out()
        bwpath = fni.agg_bw()
        dstpre = fna.fname('')
        covciname = fna.fname('covci.txt.gz') # register as temp files, delete later
        gcovname = fna.fname('gcov.txt.gz')
        ecovname = fna.fname('ecov.txt.gz')

        # if not hasattr(self, 'ex0'):
        #     self.ex0 = UT.read_pandas(expath)
        # if not hasattr(self.ex0, 'ecov'):
        #     ecov = CC.calc_ecov(expath, cipath, bwpath, dstpre, override=False, np=pr['np'])            
        # else:
        #     ecov = self.ex0
        ecov = CC.calc_ecov(expath, cipath, bwpath, dstpre, override=False, np=pr['np'])
        gcov = CC.calc_gcov(expath, cipath, bwpath, dstpre, override=False, np=pr['np'])
        
        # set ecov, gcov columns
        i2g = UT.df2dict(gcov, '_gidx','gcov')
        i2e = UT.df2dict(ecov, 'eid','ecov')
        ex0 = self.ex0
        ex0['ecov'] = [i2e[x] for x in ex0['_id']]
        ex0['gcov'] = [i2g[x] for x in ex0['_gidx']]

        # set cov column for genes
        genes0 = self.genes0
        # _gidx not in genes0, [TODO] other method? This is very fragile. 
        def name2gidx(s):
            if s[:3]=='JNG':
                return -int(s[3:])
            if s[:3]=='JPG':
                return int(s[3:])
            return int(s[2:])
        genes0['_gidx'] = [name2gidx(x) for x in genes0['name']]
        genes0['cov'] = [i2g[x] for x in genes0['_gidx']]
        
        # make datacol
        if self.datacode:
            ex0['ecov_'+self.datacode] = ex0['ecov']
            ex0['gcov_'+self.datacode] = ex0['gcov']
        # overwrite ex0, genes0
        UT.write_pandas(ex0, fna.ex_out('txt'), 'h')
        UT.write_pandas(genes0, fna.genes_out('txt'), 'h')

    def assign_sjcnt(self):
        """ calculate junction counts """
        fna = self.fna
        fni = self.fni

        sjg = GGB.read_sj(fni.sj0_bed())
        sjg['locus'] = UT.calc_locus_strand(sjg)

        l2u = UT.df2dict(sjg, 'locus','ucnt')
        l2m = UT.df2dict(sjg, 'locus','mcnt')
        if hasattr(self, 'sj0'):
            sj0 = self.sj0 
        else:
            self.sj0 = sj0 = UT.read_pandas(fna.sj_out('txt'))
        if 'locus' not in sj0.columns:
            sj0['locus'] = UT.calc_locus_strand(sj0)
        sj0['ucnt'] = [l2u.get(x,0) for x in sj0['locus']]
        sj0['mcnt'] = [l2m.get(x,0) for x in sj0['locus']]
        sj0['jcnt'] = [x or y for x,y in sj0[['ucnt','mcnt']].values]
        if self.datacode:
            for c in ['ucnt','mcnt','jcnt']:
                sj0[c+'_'+self.datacode] = sj0[c]
        # overwrite sj0
        UT.write_pandas(sj0, fna.sj_out('txt'), 'h')


    def make_unionex(self):
        self.ugb = UT.make_unionex(self.ex0, '_gidx')
        fname = self.fna.fname('unionex.txt.gz', category='output')
        UT.write_pandas(self.ugb, fname,'h')



def link_data(pre, src, dst, which='bs', tgts=[]):
    bwtgts = ['.allsample.bw', '.ex.men.bw', '.ex.mep.bw', '.ex.se.bw']
    sjtgts = ['.allsj.stats.txt.gz','.allsj.txt.gz','.sj0.bed.gz']
    if len(tgts)==0:
        if 'b' in which:
            tgts+=bwtgts
        if 's' in which:
            tgts+=sjtgts
    for suf in tgts:
        a = pre+src+suf
        b = pre+dst+suf
        cmd = ['ln','-s', a, b]
        print(' '.join(cmd))
        if os.path.exists(b):
            os.unlink(b)
        ret = subprocess.call(cmd)
        if ret !=0:
            print('error: {0}'.format(ret))
    
# below tested in 2016-05-09-collect-covs.ipynb
def covtype2path(prefix, acode, which):
    if which=='ecov':
        return prefix+'.{0}.ecov.txt.gz'.format(acode)
    if which=='gcov':
        return prefix+'.{0}.gcov.txt.gz'.format(acode)
    if which=='uecov':
        return prefix+'.{0}.uniq.ecov.txt.gz'.format(acode)
    if which=='ugcov':
        return prefix+'.{0}.uniq.gcov.txt.gz'.format(acode)
    if which=='gcov1k':
        return prefix+'.{0}1k.gcov.txt.gz'.format(acode)
    if which=='ugcov1k':
        return prefix+'.{0}1k.uniq.gcov.txt.gz'.format(acode)
    raise ValueError('unknown cov type: {0}'.format(which))

def collect_covs_worker(eidf, subsi, acode, which, dstpath):
    if which in ['ecov','uecov']:
        idcol = 'eid'
        covcol = 'ecov'
        ids = eidf['_id'].values
        eidf = eidf.set_index('_id')
    else:
        idcol = '_gidx'
        covcol = 'gcov'
        ids = eidf['_gidx'].values
        eidf = eidf.set_index('_gidx')

    for sname, covpre in subsi[['name','covpre']].values:
        covpath = covtype2path(covpre, acode, which)
        cov = UT.read_pandas(covpath)
        eidf[sname] = cov.set_index(idcol).ix[ids][covcol].values

    UT.write_pandas(eidf, dstpath, 'ih')

    return dstpath

def collect_covs(dataset_code, si, assembly_code, sjexpre, which, outdir, np=7):
    """
    Args:
        dataset_code: identifier to indicate dataset
        si: dataset sampleinfo dataframe 
         (required cololums: name, covpre=prefix to cov files)
        assembly_code: identifier for assembly
        sjexpre: assembly sjex path prefix
        which: one of ecov,gcov,uecov,ugcov,gcov1k,ugcov1k
        outdir: output directory

    """
    ex = UT.read_pandas(sjexpre+'.ex.txt.gz')
    # index
    if which in ['ecov','uecov']:
        idf = ex[['_id']].copy()
    else:
        idf = ex.groupby('_gidx')[['chr']].first().reset_index().sort_values('_gidx')
        idf = idf[['_gidx']].copy()

    dstpre = os.path.join(outdir, '{0}.{1}'.format(dataset_code, assembly_code))
    batchsize = int(N.ceil(len(si)/float(np)))
    args = []
    files = []
    si1 = si[['name','covpre']]
    for i in range(np):
        subsi = si1.iloc[i*batchsize:(i+1)*batchsize].copy()
        dstpath = dstpre+'.{0}.part{1}.txt.gz'.format(which, i)
        files.append(dstpath)
        args.append((idf, subsi, assembly_code, which, dstpath))

    rslts = UT.process_mp(collect_covs_worker, args, np=np, doreduce=False)

    # concat part files
    dfs = []
    for fpath in files:
        dfs.append(UT.read_pandas(fpath, index_col=[0]))
    df = PD.concat(dfs, axis=1)

    dstpath = dstpre+'.{0}.txt.gz'.format(which)
    UT.write_pandas(df, dstpath, 'ih')
    
    for fpath in files:
        os.unlink(fpath)
    

def collect_sjcnts_worker(idf, subsi, acode, which, dstpath):
    # idf ['_id', 'locus']
    idf = idf.set_index('_id')
    cols = []
    for sname, sjpath in subsi[['name','sjbed_path']].values:
        sj = GGB.read_sj(sjpath)
        sj['locus'] = UT.calc_locus_strand(sj)
        if which=='jcnt':
            sj['jcnt'] = [x or y for x,y in sj[['ucnt','mcnt']].values]
        l2u = UT.df2dict(sj, 'locus', which)
        idf[sname] = [l2u.get(x,0) for x in idf['locus']]        
        cols.append(sname)
    UT.write_pandas(idf[cols], dstpath, 'ih') # don't want non-sample columns
    return dstpath

def collect_sjcnts(dataset_code, si, assembly_code, sjexpre, which, outdir, np=7):
    """
    Args:
        dataset_code: identifier to indicate dataset
        si: dataset sampleinfo dataframe 
         (required cololums: name, sjbed_path=path to (converted) raw juncton count file)
        assembly_code: identifier for assembly
        sjexpre: assembly sjex path prefix
        which: ucnt, mcnt, jcnt=ucnt or mcnt (when ucnt=0)
        outdir: output directory

    """    
    sj = UT.read_pandas(sjexpre+'.sj.txt.gz')
    sj['locus'] = UT.calc_locus_strand(sj)
    idf = sj[['_id', 'locus']].copy()
    dstpre = os.path.join(outdir, '{0}.{1}'.format(dataset_code, assembly_code))
    batchsize = int(N.ceil(len(si)/float(np)))
    args = []
    files = []
    si1 = si[['name','sjbed_path']]
    for i in range(np):
        subsi = si1.iloc[i*batchsize:(i+1)*batchsize].copy()
        dstpath = dstpre+'.{0}.part{1}.txt.gz'.format(which, i)
        files.append(dstpath)
        args.append((idf, subsi, assembly_code, which, dstpath))

    rslts = UT.process_mp(collect_sjcnts_worker, args, np=np, doreduce=False)

    # concat part files
    dfs = []
    for fpath in files:
        dfs.append(UT.read_pandas(fpath, index_col=[0]))
    df = PD.concat(dfs, axis=1)

    dstpath = dstpre+'.{0}.txt.gz'.format(which)
    UT.write_pandas(df, dstpath, 'ih')
    
    for fpath in files:
        os.unlink(fpath)
    


