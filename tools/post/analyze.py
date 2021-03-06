#!/usr/bin/env python

""" Analyze the XML generated by the Compiler Analysis pass.

    This module contains classes that do various types of analysis over
    the code, including flop count, state and streaming variable access,
    working set, and memory traffic estimates.
"""
__author__ = "Cy Chan"
__copyright__ = "Copyright 2016, The Regents of the University of California, through Lawrence Berkeley National Laboratory"
__credits__ = ["Cy Chan"]
__license__ = "Modified BSD License (see LICENSE file)"
__version__ = "2.0"
__maintainer__ = "Cy Chan"
__email__ = "cychan@lbl.gov"
__status__ = "Production"

import sys
import os
import re
import operator
from copy import deepcopy

from parser import XMLParser, KeyValXMLParser, PollyXMLParser, Collection, Flops, Scalar, Array, ArrayAccess, Conditional
from box import Box
from common import options

# Helper functions

def get_type_byte_n(word_type, machine):
  return machine[word_type + "_byte_n"]

def get_cache_byte_n(machine):
  return machine['cache_kbytes'] * 1024

def get_scale_factor(tag, machine):
  return machine[tag + '_sf']

def get_access_type(accesses):
  (r,w) = (False, False)
  for x in accesses:
    (r,w) = (r or x.reads > 0, w or x.writes > 0)
  assert (r or w) and "must have non-zero reads or writes"
  return {(True, False): 'ld',
          (False, True): 'st',
          (True, True) : 'ls'}[(r,w)]

def accessSize(accs):
  """Returns the number of points in a list of (possibly overlapping) accesses using Box library."""
  def makeBox(acc):
    # add 1 since upper bound is inclusive
    return Box(intervals = map(lambda x: (x,x+1) if type(x)==int else (x[0], x[1]+1), acc.index))
  # take the union across boxes to eliminate overlaps
  return reduce(lambda x,y: x.union(y), map(makeBox, accs)).size()

def reportReuse(linenum, ws, cache):
  rel = '<=' if ws <= cache else '> '
  print "Reuse report: loop %4d WS: %8.4g %s %.4g KiB (%8d %s %8d bytes)" % \
        (linenum, float(ws) / 2**10, rel, float(cache) / 2**10, ws, rel, cache)

# helper class for checking conditionals
class TableCondsChecker(object):
  __slots__ = ['table']
  def __init__(self, conds_table):
    if options.flag_ignore_conds:
      print "WARNING: flag_ignore_conds is True, including all conditionally executed blocks!"
    self.table = dict(conds_table)
  def check_cond(self, cond):
    if cond.condition not in self.table:
      print cond.condition
      print self.table
      raise Exception("Did not find conditional in table")
    p = self.table[cond.condition]
    return p if cond.when else (1-p)
  def check_conds(self, conds):
    conds_sats = map(lambda x: self.check_cond(x), conds)
    if options.flag_verbose_conditionals and len(conds) > 0:
      print "Conditions", map(lambda x: x.condition, conds), ":", conds_sats
    return reduce(operator.mul, conds_sats, 1.0)
  def __call__(self, x):
    # x is either a Conditional or a list of Conditionals
    if options.flag_ignore_conds:
      return True
    if type(x) == Conditional:
      return self.check_cond(x)
    else:
      assert type(x) == list
      return self.check_conds(x)

# Analysis classes

class FlopCount(object):
  slots = ['name', 'adds', 'multiplies', 'divides', 'specials']
  def __init__(self, flops):
    self.name = ''
    self.adds = flops.adds
    self.multiplies = flops.multiplies
    self.divides = flops.divides
    self.specials = flops.specials
  def loop(self, loop, siblings):
    return self * loop.iter_n()
  def __str__(self):
    return "FlopCount A=%s, M=%s, D=%s, S=%s" % \
           (self.adds, self.multiplies, self.divides, self.specials)
  def __iadd__(self, other):
    self.adds += other.adds
    self.multiplies += other.multiplies
    self.divides += other.divides
    self.specials += other.specials
    return self
  def __mul__(self, n):
    result = FlopCount(self)
    result.adds *= n
    result.multiplies *= n
    result.divides *= n
    result.specials *= n
    return result

  @staticmethod
  def collector(conds_chk):
    # capture condition checker
    def f(arg, conds):
      assert type(arg) == Flops or type(arg) == Scalar or type(arg) == Array
      conds_sat = conds_chk(conds)
      if type(arg) == Flops and conds_sat > 0.0:
        return Collection([FlopCount(arg) * conds_sat])
      return Collection()
    return f


class StateVar(object):
  slots = ['name', 'type', 'reads', 'writes']
  def __init__(self, sv=None, array=None, access=None):
    if sv:
      self.name = sv.name
      self.type = sv.type
      self.reads = sv.reads
      self.writes = sv.writes
    elif array and not access:
      # array pointer
      self.name = array.name.split('.')[0]
      self.type = array.type + ' *'
      self.reads = sum(map(lambda x: x.reads + x.writes, array.accesses))
      self.writes = 0
    elif array and access:
      # array element (non-loop varying)
      self.name = array.name + str(access.index)
      self.type = array.type
      self.reads = access.reads
      self.writes = access.writes
    else:
      raise Exception("unsupported constructor call")
  def loop(self, loop, siblings):
    return self * loop.iter_n()
  def __str__(self):
    return "SV %s %s, R=%s, W=%s" % (self.type, self.name, self.reads, self.writes)
  def __iadd__(self, other):
    assert other.name == self.name and other.type == self.type
    self.reads += other.reads
    self.writes += other.writes
    return self
  def __mul__(self, n):
    result = StateVar(self)
    result.reads *= n
    result.writes *= n
    return result

  @staticmethod
  def collector(conds_chk):
    def f(arg, conds):
      assert type(arg) == Flops or type(arg) == Scalar or type(arg) == Array
      conds_sat = conds_chk(conds)
      if type(arg) == Scalar and conds_sat > 0.0:
        return Collection([StateVar(sv=arg) * conds_sat])
      elif type(arg) == Array and conds_sat > 0.0:
        return Collection([StateVar(array=arg) * conds_sat] + \
                          map(lambda ac: StateVar(array=arg, access=ac) * conds_sat,
                              filter(ArrayAccess.isStateVar, arg.accesses)))
      return Collection()
    return f


class ArrayVar(object):
  slots = ['name', 'type', 'reads', 'writes']
  def __init__(self, array):
    if type(array) == ArrayVar:
      # copy constructor
      self.name = array.name
      self.type = array.type
      self.reads = array.reads
      self.writes = array.writes
    elif type(array == Array):
      # from parser.Array object
      self.name = array.name
      self.type = array.type
      self.reads = sum(map(lambda x: 0 if x.isStateVar() else x.reads, array.accesses))
      self.writes = sum(map(lambda x: 0 if x.isStateVar() else x.writes, array.accesses))
    else:
      raise Exception("unknown arg type")
  def loop(self, loop, siblings):
    return self * loop.iter_n()
  def __str__(self):
    return "AR %s %s, L=%s, S=%s" % (self.type, self.name, self.reads, self.writes)
  def __iadd__(self, other):
    assert other.name == self.name and other.type == self.type
    self.reads += other.reads
    self.writes += other.writes
    return self
  def __mul__(self, n):
    result = ArrayVar(self)
    result.reads *= n
    result.writes *= n
    return result

  @staticmethod
  def collector(conds_chk):
    def f(arg, conds):
      assert type(arg) == Flops or type(arg) == Scalar or type(arg) == Array
      conds_sat = conds_chk(conds)
      if type(arg) == Array and not arg.onlyStateVars() and conds_sat > 0.0:
        return Collection([ArrayVar(arg) * conds_sat])
      return Collection()
    return f


class WorkingSet(object):
  """The working set associated with an array in a code region."""
  slots = ['name', 'type', 'accesses', 'word_byte_n', 'size_']
  def __init__(self, array = None, params = None, machine = None, shallow_copy = None):
    if shallow_copy:
      self.name = shallow_copy.name
      self.type = shallow_copy.type
      self.word_byte_n = shallow_copy.word_byte_n
      # shallow_copy clears accesses
      self.accesses = []
      self.size_ = None
    else:
      self.name = array.name
      self.type = array.type
      self.accesses = deepcopy(filter(lambda x: not x.isStateVar(), array.accesses))
      if params:
        self.accesses = map(lambda x: x.subParams(params), self.accesses)
      self.word_byte_n = get_type_byte_n(self.type, machine)
      self.size_ = None
  def __str__(self):
    try:
      s = "WS %s %s, words=%s, KiB=%s\n" % (self.type, self.name, self.size(), self.bytes() / 2.**10)
    except:
      s = "WS %s %s\n" % (self.type, self.name)
    for ac in self.accesses:
      s += str(ac) + '\n'
    return s
  def __iadd__(self, other):
    assert other.name == self.name and other.type == self.type
    map(self.consume, other.accesses)
    return self
  def size(self):
    # memoize
    if not self.size_:
      self.size_ = accessSize(self.accesses)
    return self.size_
  def bytes(self):
    return self.size() * self.word_byte_n
  def consume(self, acc):
    # access regions can overlap
    self.accesses.append(acc)
    self.size_ = None
  def loop(self, loop, siblings = None):
    """Unroll along loop variable, combining accesses along loop dimension."""

    def tupleDel(t, i):
      return tuple(t[:i] + t[i+1:])
    def tupleIns(t, i, v):
      return tuple(t[:i] + (v,) + t[i:])
    def appendToDict(d, k, v):
      if k not in d:
        d[k] = []
      d[k].append(v)

    result = WorkingSet(shallow_copy=self)
    buckets = {}
    for acc in self.accesses:
      if loop.loopvar not in acc.loopvars:
        result.consume(acc) # nothing to unroll
      else:
        # sort accesses into buckets to be merged
        i = acc.loopvars.index(loop.loopvar) # which index corresponds to the loopvar
        key = (i, tupleDel(acc.index, i), tupleDel(acc.loopvars, i)) # remove the index corresponding to the loopvar
        # add the index to the key's bucket along with read/write flags
        appendToDict(buckets, key, (acc.index[i], acc.reads, acc.writes))

    # each bucket now contains a set of accesses that differ only in the dimension of the loop
    # these accesses can be merged, conservatively, by taking the minimum and maximum offset
    for ((i, idx, lvars), offsets_and_rw) in buckets.iteritems():
      (offsets, reads, writes) = zip(*offsets_and_rw)
      # insert an access interval covering the looped over accesses
      (lb, ub) = loop.range
      r = (lb + min(offsets), ub + max(offsets))
      idx = tupleIns(idx, i, r)
      lvars = tupleIns(lvars, i, '')
      result.consume(ArrayAccess(index=idx, loopvars=lvars, reads=sum(reads), writes=sum(writes)))

    return result

  @staticmethod
  def collector(conds_chk, machine):
    def f(arg, conds):
      assert type(arg) == Flops or type(arg) == Scalar or type(arg) == Array
      # TODO: do something more sophisticated when Pr[conds] < 1.0
      # for now, just include it in the working set if Pr[conds] > 0.0
      if type(arg) == Array and not arg.onlyStateVars() and conds_chk(conds) > 0.0:
        return Collection([WorkingSet(arg, machine=machine)])
      return Collection()
    return f


class TrafficRegion(object):
  """The memory traffic associated with a list of regions and a count for how
     many times those regions are accessed."""
  slots = ['accesses', 'size', 'count']
  def __init__(self, accesses, count = 1):
    self.accesses = accesses
    self.size = accessSize(self.accesses)
    self.count = count
  def __str__(self):
    s = " TrafficRegion, size=%d, count=%g, acc_type=%s:\n" % \
        (self.size, self.count, get_access_type(self.accesses))
    for acc in self.accesses:
      s += str(acc) + '\n'
    return s
  def __imul__(self, n):
    self.count *= n
    return self
  def words(self):
    return self.size * self.count
  def bytes(self, acc_byte_n):
    return self.words() * acc_byte_n[get_access_type(self.accesses)]


class Traffic(object):
  """The memory traffic required by an array during execution of a code region.
    
     Contains several TrafficRegions and the working set to determine reuse in loops."""
  slots = ['name', 'element_type',
           'regions', 'ws', 'ws_block_n',
           'element_byte_n', 'acc_byte_n', 
           'params', 'block_params', 'cache_byte_n',
           'conds_chk']
  def __init__(self, array=None, params=None, block_params=None, machine=None,
               conds_chk=None, conds_sat=None, copy=None):
    if copy:
      # make a copy (excluding traffic regions and WorkingSet info)
      self.name = copy.name
      self.element_type = copy.element_type
      self.element_byte_n = copy.element_byte_n
      self.acc_byte_n = copy.acc_byte_n     # bytes per load/store access
      self.params = copy.params             # for problem size and others
      self.block_params = copy.block_params # for cache blocking only
      self.cache_byte_n = copy.cache_byte_n # cache size
      self.conds_chk = copy.conds_chk
    else:
      # copy from an Array object
      self.name = array.name
      self.element_type = array.type

      # copy non-state var accesses, do parameter substitution
      accesses = filter(lambda x: not x.isStateVar(), array.accesses)
      accesses = map(lambda x: x.subParams(params), accesses)

      # traffic regions accessed
      # conds_sat is accumulated percentage of *all* branches taken up code tree
      self.regions = [TrafficRegion(accesses, conds_sat)]

      # need to track working set to compute data reuse
      self.ws = WorkingSet(array, params=params, machine=machine)
      self.ws_block_n = 1 # single iteration

      # sizes of elements and accesses
      self.element_byte_n = get_type_byte_n(self.element_type, machine)
      self.acc_byte_n = {}
      for acc_type in ['ld', 'st', 'ls']:
        self.acc_byte_n[acc_type] = get_scale_factor(acc_type, machine) * \
                                    self.element_byte_n

      # other parameters
      self.params = params
      self.block_params = block_params
      self.cache_byte_n = get_cache_byte_n(machine)
      self.conds_chk = conds_chk # may need to re-evaluate branch taken percentage

  def __str__(self):
    s = "MT %s %s, size=%d, words=%g, bytes=%g\n" % \
        (self.element_type, self.name, self.size(), self.words(), self.bytes())
    for region in self.regions:
      s += str(region)
    return s
  def __iadd__(self, other):
    assert other.name == self.name and other.element_type == self.element_type
    self.ws += other.ws # add other's working set to ours
    map(self.consume, other.regions)
    return self
  def __imul__(self, n):
    for region in self.regions:
      region *= n
    return self
  def consume(self, region):
    # NOTE: Regions consumed may overlap, but should not be merged (yet).
    #       If cache is sufficient, a new region will be created in loop()
    #         from the embedded WorkingSet object.
    #       If cache is insufficient, each region is assumed to be loaded
    #         into cache independently (conservative estimate)
    self.regions.append(region)
  def size(self):
    return sum(map(lambda x: x.size, self.regions))
  def words(self):
    return sum(map(lambda x: x.words(), self.regions))
  def bytes(self):
    return sum(map(lambda x: x.bytes(self.acc_byte_n), self.regions))
  def loop(self, origLoop, siblings):
    """If enough cache for reuse within a block, traffic equals the blocked working set times the number of blocks.

       Otherwise, multiply traffic per iteration by number of iterations."""

    # total working set for all arrays touched in the loop (across all siblings) for one iteration
    # siblings are all different arrays, so assume no aliasing (overlap) between them
    loop_ws_byte_n = sum(map(lambda x: x.ws.bytes(), siblings))

     # only need to substitute params for the loop bounds
    loop = origLoop.subParams(self.params, shallow=True)

    # block loop if range matches
    blockLoop = origLoop.blocked(self.block_params)
    # substitute rest of parameters
    blockLoop = blockLoop.subParams(self.params, shallow=True)

    # number of blocks in the current loop dimension
    numBlocks = float(loop.range[1]      - loop.range[0]+1) / \
                (blockLoop.range[1] - blockLoop.range[0]+1)

    result = Traffic(copy = self)
    result.ws = self.ws.loop(blockLoop) # working set of the blocked loop
    result.ws_block_n = self.ws_block_n * numBlocks # number of blocks total

    # only report if we are the first of siblings (prevent printing duplicate reports)
    if self == siblings.iterfirst():
      reportReuse(loop.linenum, loop_ws_byte_n, self.cache_byte_n)
#     for s in sorted(siblings, key=lambda x: x.name):
#       print s.ws
    if loop_ws_byte_n <= self.cache_byte_n:
      # WS of all siblings fit in cache
      # traffic equals blocked working set times number of total blocks
      # assumes no reuse between blocks (i.e. problem blocked correctly for cache)
      result.regions = [TrafficRegion(result.ws.accesses, count=result.ws_block_n)]
      # re-evaluate percentage branches taken up to this loop
      result *= self.conds_chk(origLoop.conds)
    else:
      # no reuse across iterations, multiply traffic by iteration count
      result.regions = self.regions
      result *= loop.iter_n()
    return result

  @staticmethod
  def collector(conds_chk, params, block_params, machine):
    # capture problem size, blocking params, and machine model
    def f(arg, conds):
      assert type(arg) == Flops or type(arg) == Scalar or type(arg) == Array
      conds_sat = conds_chk(conds)
      if type(arg) == Array and not arg.onlyStateVars() and conds_sat > 0.0:
        return Collection([Traffic(arg, params, block_params, machine, conds_chk, conds_sat)])
      return Collection()
    return f

class StaticAnalysis(object):

  slots = ['functions', 'scops']

  def __init__(self, xml, polly_xml = None, symsubs = None, namesubs = None):
    self.functions = XMLParser(xml, symsubs, namesubs).functions
    if polly_xml:
      self.scops = PollyXMLParser(polly_xml).scops
    else:
      self.scops = []

  def dump(self, params, block_params, machine, conds_chk, flag_sub_params):
    for function in self.functions:
      print "*" * (4+len(function.name))
      print "* %s *" % function.name
      print "*" * (4+len(function.name))
      for sym_loop in function.body.loops:
        print
        print "*" * (9+len(str(sym_loop.linenum)))
        print "* Loop %d *" % sym_loop.linenum
        print "*" * (9+len(str(sym_loop.linenum)))
        print

        if flag_sub_params:
          loop = sym_loop.subParams(params)
          block_loop = sym_loop.blocked(block_params)
          block_loop = block_loop.subParams(params)
        else:
          loop = sym_loop
          block_loop = sym_loop

        print "Floating Point Ops (A/S/M/D):"
        print loop.collect(FlopCount.collector(conds_chk))

        print "State Variables (R/W):"
        print loop.collect(StateVar.collector(conds_chk))

        print "Array Variables (L/S):"
        print loop.collect(ArrayVar.collector(conds_chk))

        print "Working Set:"
        print block_loop.collect(WorkingSet.collector(conds_chk, machine))

        print "Memory Traffic:"
        mt = sym_loop.collect(Traffic.collector(conds_chk, params, block_params, machine))
        total_bytes = sum(map(lambda x: x.bytes(), mt))
        print
        print "Total Memory Traffic (L/S) using cache model: %g GiB (%g bytes)" % \
              (float(total_bytes) / 2**30, total_bytes)
        print
        print mt
