"""
.. module:: gtfgffbed
    :synopsis: GTF/GFF/BED related functions

..  moduleauthor:: Ken Sugino <ken.sugino@gmail.com>

"""

import csv
import subprocess
import os
import gzip
import logging
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)

import pandas as PD
import numpy as N

from jgem import utils as UT


GTFCOLS = ['chr','src','typ','st','ed','sc1','strand','sc2','extra']
GFFCOLS = ['chr','src','typ','st','ed','sc1','strand','sc2','attr']
BEDCOLS = ['chr', 'st', 'ed', 'name', 'sc1', 'strand', 'tst', 'ted', 'sc2', '#exons', 'esizes', 'estarts']
DEFAULT_GTF_PARSE = ['gene_id','transcript_id','exon_number','gene_name','cov']

# SJ.out.tab to SJBED ###################################################################

def sjtab2sjbed(sjtab, sjbed, aligned):
    """Generate splice junction input file from STAR SJ.out.tab

    Args:
        sjtab (str): path to SJ.out.tab file
        sjbed (str): path to output bed
        aligned (int): mapped reads 

    Returns:
        Pandas dataframe

    """
    SJCOLS = ['chr','st','ed','strand2','motif','annotated','ureads','mreads','maxoverhang']
    SJMOTIF = {0:'non-canonical',1:'GT/AG',2:'CT/AC',3:'GC/AG',4:'CT/GC',5:'AT/AC',6:'GT/AT'}
    SJSTRAND = {1:'+',2:'-',0:'.'}
    sj = PD.read_table(sjtab, names=SJCOLS)
    sj['name'] = ['%s-k%d-u%d-m%d-o%d' % (SJMOTIF[x], a, u, m, o) for x,a, u,m,o in \
                  sj[['motif','annotated','ureads','mreads','maxoverhang']].values]
    sj['strand'] = [SJSTRAND[x] for x in sj['strand2']]
    scale = 1e6/float(aligned)
    sj['ucnt'] = sj['ureads']*scale
    sj['mcnt'] = sj['mreads']*scale
    #sj['jcnt'] = [x or y for x, y in sj[['ucnt','mcnt']].values]
    #cols = ['chr','st','ed','name','strand','ucnt','mcnt']#,'jcnt']
    #UT.write_pandas(sj[cols], sjbed, '')
    sj['sc1'] = sj['ureads']*scale
    sj['tst'] = sj['mreads']*scale
    #cols = ['chr','st','ed','name','sc1','strand','tst'] 
    #UT.write_pandas(sj[cols], sjbed, '')
    write_bed(sj, sjbed, ncols=7)
    return sj



# READ/WRITE       ######################################################################    
def get_gff_attr_col(gff, aname):
    "helper function for read_gff"
    return [dict([y.split('=') for y in line.split(';') if '=' in y]).get(aname,'') for line in gff['attr']]

def read_gff(gffname, onlytypes=[], parseattrs=[]):
    """ Read in whole GFF, parse id & parent

    Args:
        gffname: path to GFF file
        onlytypes: only keep these types. If [] or None, then keep all (default). 
        parseattrs: extra attributes to parse

    Returns:
        Pandas.DataFrame containing GFF data
    
    """
    if not UT.isstring(gffname):
        return gffname
        
    if gffname.endswith('.gz'):
        gff = PD.read_table(gffname, names=GFFCOLS, comment='#', compression='gzip')
    else:
        gff = PD.read_table(gffname, names=GFFCOLS, comment='#')

    for c in ['ID','Parent']+parseattrs:
        gff[c] = get_gff_attr_col(gff, c)

    # set gid, tid, eid by default
    gff['gid'] = ''
    gff['tid'] = ''
    gff['eid'] = ''
    # genes
    gidx = gff['typ']=='gene'
    gff.ix[gidx, 'gid'] = gff['ID']
    # transcripts
    tidx = gff['typ']=='transcript'
    gff.ix[tidx, 'gid'] = gff['Parent']
    gff.ix[tidx, 'tid'] = gff['ID']
    # exons
    tid2gid = dict(gff[tidx][['tid','gid']].values)
    eidx = gff['typ']=='exon'
    gff.ix[eidx, 'tid'] = gff['Parent']
    gff.ix[eidx, 'gid'] = [tid2gid[x] for x in gff[eidx]['tid'].values]
    if N.sum(gff[eidx]['ID']=='')==0: # ID already set
        gff.ix[eidx, 'eid'] = gff['ID']
    else: # sometimes exons don't have ID => create
        edf = gff[eidx]
        en = edf.groupby('tid')['gid'].transform(lambda x: N.arange(1,len(x)+1))
        eids = edf['tid']+':'+ en.astype(str)
        gff.ix[eidx, 'eid'] = eids
        attr = 'ID='+eids+';Parent='+edf['Parent']
        gff.ix[eidx, 'attr'] = attr

    if onlytypes:
        gff = gff[gff['typ'].isin(onlytypes)]

    return gff

# this function is awfully slow => replace with vectorized version
# def get_gtf_attr_col(gtf, aname):
#     "helper function for read_gtf"
#     def _attr(line):
#         #if isinstance(line, basestring):
#         #if isinstance(line, str):
#         if UT.isstring(line):
#             dic = dict([(x[0],x[1][1:-1]) for x in [y.split() for y in line.split(';')] if len(x)>1])
#             return dic.get(aname,'')
#         return ''
#     return [_attr(line) for line in gtf['extra']]

def read_gtf(gtfname, onlytypes=['exon'], parseattrs=DEFAULT_GTF_PARSE, rename={}):
    """ Read in whole GTF, parse gene_id, transcript_id from column 9

    Args:
        gtfname: path to GTF file
        onlytypes: only keep these types. If [] or None, then keep all (default).
        parseattrs: which column attributes to parse.

    Returns:
        Pandas DataFrame containing GTF data

    """
    if not UT.isstring(gtfname):
        return gtfname
        
    if gtfname.endswith('.gz'):
        gtf = PD.read_table(gtfname, names=GTFCOLS, compression='gzip', comment='#')
    else:
        gtf = PD.read_table(gtfname, names=GTFCOLS, comment='#')
    if onlytypes:
        gtf = gtf[gtf['typ'].isin(onlytypes)].copy()
    LOG.debug( "extracting ids...")
    # field1 "field1 value"; field2 "field2 value"; ...
    # 14.283 sec (using get_gtf_attr_col) ==> 5.830 sec (using below)
    tmp = gtf['extra'].str.split(';',expand=True) # each column: field "field val"
    cols = gtf[['chr']].copy()
    for c in tmp.columns:
        kv = tmp[c].str.split(expand=True) # key col(0) and value col(1)
        #LOG.debug((kv[0].unique(),kv.shape, kv.columns))
        LOG.debug(kv[0].unique())
        if len(kv[0].unique())==1:
            k = kv.iloc[0][0]
            if UT.isstring(k) and kv.shape[1]==2:
                cols[k] = kv[1].str.replace('"','') # strip "
        else: # multiple  fields => make cols for each
            for k in kv[0].unique():
                if UT.isstring(k):
                    idx = kv[0]==k
                    cols.loc[idx, k] = kv[1][idx].str.replace('"','')

    for c in parseattrs:
        if c in cols:
            gtf[c] = cols[c] # get_gtf_attr_col(gtf, c)
        else:
            LOG.warning('column {0} not found'.format(c))
    if rename:
        gtf = gtf.rename(columns=rename)
    return gtf

def read_bed(fpath, calcextra=False):
    """Read BED file

    Args:
        fpath: path to BED file
        calcextra: calculate extra fields (locus, min.exon.size, max.exon.size, length)

    Returns:
        Pandas DataFrame containing BED data

    """
    if not UT.isstring(fpath):
        return fpath

    if fpath.endswith('.gz'):
        d = PD.read_table(fpath, header=None, compression='gzip')
        d.columns = BEDCOLS[:len(d.columns)]
    else:
        d = PD.read_table(fpath, header=None)
        d.columns = BEDCOLS[:len(d.columns)]
    if calcextra:
        d['locus'] = d['chr'].astype(str) + ':'+ d['st'].astype(str)+'-'+ d['ed'].astype(str)
        d['min.exon.size'] = d['esizes'].apply(lambda x: N.min(list(map(int, x[:-1].split(',')))))
        d['max.exon.size'] = d['esizes'].apply(lambda x: N.max(list(map(int, x[:-1].split(',')))))
        d['length'] = d['ed']-d['st']
    return d

def write_gff(df, fname, compress=True):
    """Write GFF file.

    Args:
        df: Pandas.DataFrame containing GFF data
        fname: path 
        compress: whether to gzip compress (default:True)

    Returns:
        actual path written
    """
    return write_ggb(df, fname, GFFCOLS, compress)
    
def write_gtf(df, fname, compress=True):
    """Write GTF file.

    Args:
        df: Pandas.DataFrame containing GTF data
        fname: path 
        compress: whether to gzip compress (default:True)

    Returns:
        actual path written
    """
    return write_ggb(df, fname, GTFCOLS, compress)
    
def write_bed(df, fname, compress=True, ncols=6):
    """Write BED file.

    Args:
        df: Pandas.DataFrame containing BED data
        fname: path 
        compress: whether to gzip compress (default:True)
        ncols: number of bed columns (default 12)

    Returns:
        actual path written
    """ 
    return write_ggb(df, fname, BEDCOLS[:ncols], compress)
    
def write_ggb(df, fname, cols, compress=True):    
    # df.loc[:,'st'] = df['st'].astype(int)
    # df.loc[:,'ed'] = df['ed'].astype(int)
    if (df.dtypes['st'] != int) or (df.dtypes['ed'] != int):
        LOG.warning('st,ed not integer: copy and converting')
        df = df.copy()
        df['st'] = df['st'].astype(int)
        df['ed'] = df['ed'].astype(int)
    if fname[-3:]=='.gz':
        fname = fname[:-3]
    df[cols].to_csv(fname, index=False, header=False, sep='\t', quoting=csv.QUOTE_NONE)
    if compress:
        return UT.compress(fname)
    return fname


# CONVERSION     ######################################################################

def gtf2gff(gtfname,gffname, memt=True):
    """Convert GTF to GFF.

    Args:
        gtfname: path to GTF file
        gffname: path for converted GFF file
        memt: only select multiexon, multitranscript

    Returns:
        Pandas.DataFrame containing converted GFF data
    """
    eids = read_gtf(gtfname, 
                    onlytypes=['exon'], 
                    parseattrs=['gene_id','transcript_id','exon_number','gene_name'],
                    rename={'gene_id':'gid','transcript_id':'tid','gene_name':'gname','exon_number':'e#'})
    if N.sum(eids['e#']=='')>0: # recalculate exon_number
        eids['e#'] = eids.groupby('tid')['gid'].transform(lambda x: N.arange(1,len(x)+1))
    else:
        eids['e#'] = eids['e#'].astype(int)
    eids['ID'] = eids['tid']+':'+eids['e#'].astype(str)
    eids['attr'] = 'ID='+eids['ID']+';Parent='+eids['tid']

    # groupby tid and get transcript records
    LOG.debug( "calculating transcripts...")
    gtid = eids.groupby('tid')
    tids = gtid.first().copy() # in general get first record
    tids['typ'] = 'transcript'  # fix typ
    tids['st'] = gtid['st'].min() # fix st
    tids['ed'] = gtid['ed'].max() # fix ed
    tids['#exons'] = gtid.size()
    if memt:
        tids = tids[tids['#exons']>1]
    tids = tids.reset_index()
    tids['e#'] = 0
    tids['attr'] = 'ID='+ tids['tid']+';Parent='+tids['gid']+\
                   ';num_exons='+tids['#exons'].astype(str)+\
                   ';gene_name='+tids['gname']

    # groupby gid and get gene records
    LOG.debug( "calculating genes...")
    ggid = tids.groupby('gid')
    gids = ggid.first().copy()
    gids['typ'] = 'gene'
    gids['st'] = ggid['st'].min()
    gids['ed'] = ggid['ed'].max()
    gids['#trans'] = ggid.size()
    gids = gids.reset_index()
    if memt:
        gids = gids[gids['#trans']>1] # multi transcript
        tids = tids[tids['gid'].isin(gids['gid'].values)]
        eids = eids[eids['tid'].isin(tids['tid'].values)]
    gids['tid'] = ''
    gids['e#'] = -1
    gids['attr'] = 'ID='+gids['gid']+';num_trans='+gids['#trans'].astype(str)

    LOG.debug( "merging exons, transcripts, genes...")
    gte = PD.concat([gids,tids,eids],ignore_index=True)
    # sort by gid,tid,st,ed
    gte = gte.sort_values(['chr','gid','tid','e#'])
    # write out
    LOG.debug( "writing GFF...")
    write_gff(gte, gffname)
    return gte

def gtf2bed12(fpath, compress=True):
    """Convert GTF to BED. Uses gtfToGenePred, genePredToBed (UCSC Kent Tools)

    Args:
        gtfname: path to GTF file
        compress: whether to gzip (default True)

    Returns:
        Pandas.DataFrame containing converted BED12 data
    """
    if fpath.endswith('.gz'):
        base = fpath[:-7]
        cmd = ['gunzip',fpath]
        LOG.debug( "expanding compressed ...", base)
        subprocess.call(cmd)
    else:
        base = fpath[:-4]
    cmd = ['gtfToGenePred','-genePredExt','-ignoreGroupsWithoutExons',base+'.gtf',base+'.gp']
    LOG.debug( "converting to GenPred...", base)
    ret = subprocess.call(cmd)
    if ret != 0:
        LOG.debug("error converting to GenPred...code{0}".format(ret))
        raise Exception
    cmd = ['genePredToBed', base+'.gp', base+'.bed']
    LOG.debug( "converting to Bed12...", base)
    ret = subprocess.call(cmd)
    if ret != 0:
        LOG.debug("error converting to GenPred...code{0}".format(ret))
        raise Exception
    os.unlink(base+'.gp')
    # gzip
    LOG.debug("gzipping ...{0}.bed".format(base))
    bdpath = base+'.bed'
    if compress:
        bdpath = UT.compress(bdpath)
    if fpath.endswith('.gz'):
        LOG.debug( "gzipping ...", fpath[:-3])
        p = subprocess.call(['gzip',fpath[:-3]])
        LOG.debug( "subprocess result", p)
    return bdpath

def bed2gtf(fpath, compress=True):
    """Convert BED to GTF. Uses bedToGenePred, genePredToGtf (UCSC Kent Tools)

    Args:
        gtfname: path to BED file
        compress: whether to gzip (default True)

    Returns:
        Pandas.DataFrame containing converted GTF data
    """
    if fpath.endswith('.gz'):
        base = fpath[:-7]
        cmd = ['gunzip',fpath]
        LOG.debug( "expanding compressed ...", base)
        subprocess.call(cmd)
    else:
        base = fpath[:-4]
    gppath = base+'.genePred'
    bdpath = base+'.gtf'
    cmd = ['bedToGenePred',base+'.bed', gppath]
    LOG.debug( "converting to GenPred...", base)
    subprocess.call(cmd)
    cmd = ['genePredToGtf','-source=.','file', gppath, bdpath]
    LOG.debug( "converting to GTF...", base)
    subprocess.call(cmd)
    os.unlink(gppath)
    # gzip
    LOG.debug( "gzipping ...", bdpath)
    if compress:
        UT.compress(bdpath)
        bdpath=bdpath+'.gz'
    if fpath.endswith('.gz'):
        LOG.debug( "gzipping ...", fpath[:-3])
        subprocess.call(['gzip',fpath[:-3]])
    return bdpath

# UTILS         ######################################################################

def chop_chrs_gtf(gtfname, chrs, outdir=None):
    """Separate chromosomes into different files.

    Args:
        gtfname: path to GTF
        chrs: list of chromosome names
        outdir: output directory, if None (default), then use same directory as input
        
    """
    #chrs = ['chr%d' % (x+1,) for x in range(19)] +['chrX','chrY']
    if outdir is None:
        outdir = os.path.dirname(gtfname)
    base = os.path.basename(gtfname)[:-4]
    outnames = [os.path.join(outdir, base+'-%s.gtf' % x) for x in chrs]
    if all([UT.notstale(gtfname, x) for x in outnames]):
        # all files already exist and newer than gtfname
        return outnames
    gtf = read_gtf(gtfname, parseattrs=[]) # don't parse attrs
    for c,fname in zip(chrs,outnames):
        LOG.debug( "writing %s to %s..." % (c, fname))
        sub = gtf[gtf['chr']==c]
        write_gtf(sub, fname, compress=False)
    return outnames    




#### FASTA/PANDAS ######################################################
def fasta2panda(fname):
    if fname.endswith('.gz'):
        fa = gzip.open(fname).read()
    else:
        fa = open(fname).read()
    def _parse(x):
        lines = x.split('\n')
        tid = lines[0].split()[0]
        seq = ''.join(lines[1:])
        return tid, seq
    recs = [_parse(x) for x in fa.split('>') if x.strip()]
    fadf = PD.DataFrame(recs, columns=['tid','seq'])
    return fadf