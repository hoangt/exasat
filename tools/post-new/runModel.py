#!/usr/bin/python

import sys
import os
from math import sqrt
from pprint import pprint

import analyze
import runner

baseXMLDir = '../../examples/cns-smc/xml'

def runDemo():
  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'advance.xml'))
  runr = runner.Runner(sa)
  for config in [{}, \
                 {'X Block Size': 4, 'Y Block Size': 4, 'Z Block Size': 4}, \
                 {'X Problem Size': 64, 'Y Problem Size': 64, 'Z Problem Size': 64}, \
                ]:
    r = runr.runModel(config)
    print "Params: %s" % str(r['params'])
    print "Comp: %g Gflops" % (float(r['flops']) / 10**9)
    print "Data: %g GB" % (float(r['BW']) / 2**30)
    print "Comp. Time: %g seconds" % r['time']
    print "Comm. Time: %g seconds" % r['cTime']
    print

def runConfig(result, sa, blockx, blocky, blockz, problemSize, cacheSizes, config = {}):
  runr = runner.Runner(sa)
  config['X Problem Size'] = problemSize
  config['Y Problem Size'] = problemSize
  config['Z Problem Size'] = problemSize
  config['X Block Size'] = blockx
  config['Y Block Size'] = blocky
  config['Z Block Size'] = blockz
  BW = []
  for cacheSize in cacheSizes:
    config['$/thread group (kB)'] = cacheSize
    r = runr.runModel(config)
    BW.append(float(r['BW']) / 2**30)
  return {'problemSize': problemSize, 'blockx': blockx, 'blocky': blocky, 'blockz': blockz, \
          'cacheSizes': cacheSizes, 'BW': BW, 'WS': float(r['WS']) / 2**10, \
          'gflops': float(r['flops']) / 10**9, 'wgflops': float(r['wflops']) / 10**9, \
          'GPRegs': r['GPRegs'], 'FPRegs': r['FPRegs'], \
          'cputime': r['cputime'], 'ramtime': r['ramtime'], 'time': r['time']}

def blockCacheTable384(sa, cacheSizes, config = {}):

# # square block sizes
# blockSizes384 = [(1,1),(2,1),(1,3),(2,2),(2,3), \
#                  (4,2),(4,3),(4,4),(4,6),(8,4),(8,6),(8,8),(8,12), \
#                  (16,8),(16,12),(16,16),(16,24),(32,16),(32,24),(32,32),(32,48), \
#                  (64,32),(64,48),(64,96),(128,96),(192,96),(192,128),(192,192), \
#                  (384,128),(384,192),(384,384)]

  # long unit-stride block sizes
  blockSizes384 = [(1,1),(2,1),(4,1),(8,1),(16,1),(32,1),(48,1),(64,1),(96,1),(128,1),(192,1), \
                   (384,1),(384,2),(384,4),(384,8),(384,16),(384,32), \
                   (384,48),(384,64),(384,96),(384,128),(384,192),(384,384)]

  result = []
  for (blockx, blocky) in blockSizes384:
    result.append(runConfig(result, sa, blockx, blocky, 384, 384, cacheSizes, config))
  return result

def blockCacheTable(sa, problemSize, cacheSizes, config = {}):
  if 'PWFlag' in config and config['PWFlag']:
    return blockCacheTableYZ(sa, problemSize, cacheSizes, config)
  else:
    return blockCacheTableXY(sa, problemSize, cacheSizes, config)

def blockCacheTableXY(sa, problemSize, cacheSizes, config = {}):
  cachelinewords = 8
  blockx = 1
  blocky = 1
  blockz = problemSize
  result = []
  while blockx <= problemSize and blocky <= problemSize:
    result.append(runConfig(result, sa, blockx, blocky, blockz, problemSize, cacheSizes, config))
    if blockx < cachelinewords or blockx <= blocky:
      blockx *= 2
    else:
      blocky *= 2
  return result

def blockCacheTableYZ(sa, problemSize, cacheSizes, config = {}):
  blockx = problemSize
  blocky = 1
  blockz = 1
  result = []
  while blocky <= problemSize and blockz <= problemSize:
    result.append(runConfig(result, sa, blockx, blocky, blockz, problemSize, cacheSizes, config))
    if blocky <= blockz:
      blocky *= 2
    else:
      blockz *= 2
  return result

def collectSMCData(dynxml, problemSize, cacheSizes, PWFlag = False):

  sa = analyze.StaticAnalysis(dynxml)
  SMC = {}
  for ns in (9, 21, 53, 71, 107):
    config = {'PWFlag': PWFlag, 'Num Species': ns}
    SMC[ns] = blockCacheTable(sa, problemSize, cacheSizes, config)

  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'lidryer.xml'))
  LID = blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': 9})

  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'drm19.xml'))
  DRM = blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': 21})

  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'grimech30.xml'))
  GRI = blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': 53})

  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'hai.xml'))
  HAI = blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': 71})

  sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'prf_ethanol.xml'))
  PRF = blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': 107})

  return (SMC, LID, DRM, GRI, HAI, PRF)

def collectData(xmlfile, problemSize, cacheSizes, species):
  sa = analyze.StaticAnalysis(xmlfile)
  return blockCacheTable(sa, problemSize, cacheSizes, {'Num Species': species})

def printBlockCacheTable(table):

  from math import sqrt

  # print header
  cacheSizes = table[0]['cacheSizes']
  print '\tCache Size (kB)'
  print "Block Size\t",
  for c in cacheSizes:
    print "%d\t" % c,
  print "Working Set (kB)\t",
  print "CPU Time (ms)\t",
  print "DRAM Time (ms)\t",
  print "Total Time (ms)\t",
  print

  for entry in table:
    # block size is square root of tile size (assuming one dim is unblocked)
    b = sqrt(entry['blockx'] * entry['blocky'] * entry['blockz'] / entry['problemSize'])
    print '%g \t' % b,
    for x in entry['BW']:
      print '%g \t' % x,
    print '%g \t'*4 % (entry['WS'], entry['cputime'], entry['ramtime'], entry['time'])
    b *= 2**0.5
  print

def printSMCBlockCacheTables(cacheTables):

  def combineResults(x, y):
    return map(lambda x1, x2: {'problemSize': x1['problemSize'], \
                               'blockx': x1['blockx'], 'blocky': x1['blocky'], \
                               'blockz': x1['blockz'], 'cacheSizes': x1['cacheSizes'], \
                               'BW': map(lambda x,y: x + y, x1['BW'], x2['BW']), \
                               'WS': max(x1['WS'], x2['WS']), \
                               'gflops': x1['gflops'] + x2['gflops'], \
                               'wgflops': x1['wgflops'] + x2['wgflops'], \
                               'GPRegs': max(x1['GPRegs'], x2['GPRegs']), \
                               'FPRegs': max(x1['FPRegs'], x2['FPRegs']), \
                               'cputime': x1['cputime'] + x2['cputime'], \
                               'ramtime': x1['ramtime'] + x2['ramtime'], \
                               'time': x1['time'] + x2['time']}, \
               x, y)

  (SMC, LID, DRM, GRI, HAI, PRF) = cacheTables
  for (chem, nspecies) in [(LID, 9), (DRM, 21), (GRI, 53), (HAI, 71), (PRF, 107)]:
    print '%d Species\n' % nspecies
    printBlockCacheTable(SMC[nspecies])
#    printBlockCacheTable(chem)
#    printBlockCacheTable(combineResults(SMC[nspecies], chem))

def collectSMCSummaryData(dynxml):

  config = {}
  config['X Problem Size'] = 128
  config['Y Problem Size'] = 128
  config['Z Problem Size'] = 128
  config['X Block Size'] = 128
  config['Y Block Size'] = 128
  config['Z Block Size'] = 128
  config['$/thread group (kB)'] = 1024

  tests = [('lidryer'    , {'$/thread group (kB)':    0, 'Num Species': 9  }), \
           ('drm19'      , {'$/thread group (kB)':    0, 'Num Species': 21 }), \
           ('grimech30'  , {'$/thread group (kB)':    0, 'Num Species': 53 }), \
           ('hai'        , {'$/thread group (kB)':    0, 'Num Species': 71 }), \
           ('prf_ethanol', {'$/thread group (kB)':    0, 'Num Species': 107}), \
           ('lidryer'    , {'$/thread group (kB)':  1e9, 'Num Species': 9  }), \
           ('drm19'      , {'$/thread group (kB)':  1e9, 'Num Species': 21 }), \
           ('grimech30'  , {'$/thread group (kB)':  1e9, 'Num Species': 53 }), \
           ('hai'        , {'$/thread group (kB)':  1e9, 'Num Species': 71 }), \
           ('prf_ethanol', {'$/thread group (kB)':  1e9, 'Num Species': 107}), \
           ('lidryer',     {'$/thread group (kB)': 1024, 'Num Species':   9, 'X Block Size': 16, 'Y Block Size': 16}), \
           ('drm19',       {'$/thread group (kB)': 1024, 'Num Species':  21, 'X Block Size': 16, 'Y Block Size':  8}), \
           ('grimech30',   {'$/thread group (kB)': 1024, 'Num Species':  53, 'X Block Size': 8 , 'Y Block Size':  8}), \
           ('hai',         {'$/thread group (kB)': 1024, 'Num Species':  71, 'X Block Size': 8 , 'Y Block Size':  4}), \
           ('prf_ethanol', {'$/thread group (kB)': 1024, 'Num Species': 107, 'X Block Size': 8 , 'Y Block Size':  4})]

  dynSA = analyze.StaticAnalysis(dynxml)

  results = []
  for (c, t) in tests:
    for k in t.keys():
      config[k] = t[k]
    chemSA = analyze.StaticAnalysis(os.path.join(baseXMLDir, '%s.xml' % c))
    results.append((dynSA.runModel(config), chemSA.runModel(config)))
    del results[-1][0]['functions']
    del results[-1][1]['functions']

  return results

def printSMCSummaryTables(results):

  def printTable(tag, op, norm = 1.0):
    print tag
    print '\t9\t21\t53\t71\t107' * (len(results) / 5)
    print 'Dynamics\t'  + '%g\t'*len(results) % tuple(map(lambda x: float(x[0][tag]) / norm, results))
    print 'Chemistry\t' + '%g\t'*len(results) % tuple(map(lambda x: float(x[1][tag]) / norm, results))
    print 'Both\t'      + '%g\t'*len(results) % tuple(map(lambda x: float(op(x[0][tag], x[1][tag])) / norm, results))
    print

  from operator import add

  printTable('flops', add, 1e9)
  printTable('wflops', add, 1e9)
  printTable('GPRegs', max)
  printTable('FPRegs', max)
  printTable('BW', add, 2**30)
  printTable('WS', max, 2**10)
  printTable('time', add, 1e-3)

def printSMCRegTables(dataType = 'double', numRegs = [0,1,2,4,8,16,32,64,128,256], dispChem = False, dispDyn = False):
  tests = [('lidryer',     {'Num Species': 9  }), # hydrogen
           ('drm19',       {'Num Species': 21 }), # methane
           ('grimech30',   {'Num Species': 53 }), # methane
           ('hai',         {'Num Species': 71 }), # propane
           ('prf_ethanol', {'Num Species': 107})] # ethanol
  dynSA = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'advance-smc-modified.xml'))
  f = open('RegAllocTables.tsv', 'wt')
  for (chem, config) in tests:
    chemSA = analyze.StaticAnalysis(os.path.join(baseXMLDir, '%s.xml' % chem))
    for nreg in numRegs:
      config[ 'FP Regs/thread'] = nreg
      config['Int Regs/thread'] = nreg
      r = runner.Runner(chemSA).runModel(config)
      runner.writeRegAllocTables(f, r, dataType)
      if dispChem:
        temp = r['functions'].values()[0]['loops'][0]['regAlloc'][dataType]
        for varType in ['state', 'stream']:
          t = temp[varType]
          for (x, y) in [('inRegs', 'inCache'), ('hits', 'misses')]:
            print '%d\t'*3 % (t[x], t[y], t[x] + t[y]),
        print
      r = runner.Runner(dynSA).runModel(config)
      runner.writeRegAllocTables(f, r, dataType)
      if dispDyn:
        temp = r['regAlloc'][dataType]
        for varType in ['state', 'stream']:
          t = temp[varType]
          print '%d\t'*3 % (t['hits'], t['misses'], t['hits'] + t['misses']),
        print

  f.close()

if __name__ == '__main__':

#  cacheSizes = [0] + [2**x for x in xrange(0, 15)] + [2**30]
  cacheSizes = [0] + [2**x for x in xrange(0, 20)] + [2**30]

  if True:
    sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'advance-flat.xml'))
    printBlockCacheTable(blockCacheTable(sa, 128, cacheSizes))

    sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'advance-fused-nomod.xml'))
    printBlockCacheTable(blockCacheTable(sa, 128, cacheSizes))

  if False:
    printSMCSummaryTables(collectSMCSummaryData( \
      os.path.join(baseXMLDir, 'advance-smc-modified.xml')))

  if False:
    printSMCBlockCacheTables(collectSMCData( \
      os.path.join(baseXMLDir, 'advance-smc-modified.xml'), \
      128, cacheSizes, PWFlag = True))
    printSMCBlockCacheTables(collectSMCData( \
      os.path.join(baseXMLDir, 'advance-smc-fused-light.xml'), \
      128, cacheSizes, PWFlag = True))
    printSMCBlockCacheTables(collectSMCData( \
      os.path.join(baseXMLDir, 'advance-smc-fused.xml'), \
      128, cacheSizes, PWFlag = True))

  if False:
    config = {'GB/s/thread': 2, 'Gflop/s/thread': 8.4, 'Threads': 24}
    for tag in ['lap', 'lap4', 'div', 'grad']:
      print '%s:' % tag
      sa = analyze.StaticAnalysis(os.path.join(baseXMLDir, 'micro/%s.xml' % tag))
      printBlockCacheTable(blockCacheTable384(sa, cacheSizes, config))

  if False:
    for xmlfile in [os.path.join(baseXMLDir, 'diffterm_compact.xml'),
                    os.path.join(baseXMLDir, 'diffterm_s3d.xml')]:
      cacheSizes = [0, 1024]
      for species in [9, 21, 53, 71, 107]:
        cacheTables = collectData(xmlfile, 128, cacheSizes, species)
        print "%d Species" % species
        printBlockCacheTable(cacheTables)

  if False:
    params = {'dataType': 'double', 'dispDyn': True}
    printSMCRegTables(**params)
