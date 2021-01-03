#!/usr/bin/env python3

# This script manages littlefs tests, which are configured with
# .toml files stored in the tests directory.
#

import toml
import glob
import re
import os
import io
import itertools as it
import collections.abc as abc
import subprocess as sp
import base64
import sys
import copy
import shlex
import pty
import errno
import signal

TEST_PATHS = 'tests'
RULES = """
define FLATTEN
%(path)s%%$(subst /,.,$(target)): $(target)
    ./scripts/explode_asserts.py $$< -o $$@
endef
$(foreach target,$(SRC),$(eval $(FLATTEN)))

-include %(path)s*.d
.SECONDARY:

%(path)s.test: %(path)s.test.o \\
        $(foreach t,$(subst /,.,$(SRC:.c=.o)),%(path)s.$t)
    $(CC) $(CFLAGS) $^ $(LFLAGS) -o $@
"""
COVERAGE_RULES = """
%(path)s.test: override CFLAGS += -fprofile-arcs -ftest-coverage

# delete lingering coverage
%(path)s.test: | %(path)s.clean
.PHONY: %(path)s.clean
%(path)s.clean:
    rm -f %(path)s*.gcda

# accumulate coverage info
.PHONY: %(path)s.info
%(path)s.info:
    $(strip $(LCOV) -c \\
        $(addprefix -d ,$(wildcard %(path)s*.gcda)) \\
        --rc 'geninfo_adjust_src_path=$(shell pwd)' \\
        -o $@)
    $(LCOV) -e $@ $(addprefix /,$(SRC)) -o $@

.PHONY: %(path)s.cumul.info
%(path)s.cumul.info: %(path)s.info
    $(LCOV) -a $< $(addprefix -a ,$(wildcard $@)) -o $@
"""
GLOBALS = """
//////////////// AUTOGENERATED TEST ////////////////
#include "lfs.h"
#include "bd/lfs_testbd.h"
#include <stdio.h>
extern const char *lfs_testbd_path;
extern uint32_t lfs_testbd_cycles;
"""
DEFINES = {
    'LFS_READ_SIZE': 16,
    'LFS_PROG_SIZE': 'LFS_READ_SIZE',
    'LFS_BLOCK_SIZE': 512,
    'LFS_BLOCK_COUNT': 1024,
    'LFS_BLOCK_CYCLES': -1,
    'LFS_CACHE_SIZE': '(64 % LFS_PROG_SIZE == 0 ? 64 : LFS_PROG_SIZE)',
    'LFS_LOOKAHEAD_SIZE': 16,
    'LFS_ERASE_VALUE': 0xff,
    'LFS_ERASE_CYCLES': 0,
    'LFS_BADBLOCK_BEHAVIOR': 'LFS_TESTBD_BADBLOCK_PROGERROR',
}
PROLOGUE = """
    // prologue
    __attribute__((unused)) lfs_t lfs;
    __attribute__((unused)) lfs_testbd_t bd;
    __attribute__((unused)) lfs_file_t file;
    __attribute__((unused)) lfs_dir_t dir;
    __attribute__((unused)) struct lfs_info info;
    __attribute__((unused)) char path[1024];
    __attribute__((unused)) uint8_t buffer[1024];
    __attribute__((unused)) lfs_size_t size;
    __attribute__((unused)) int err;
    
    __attribute__((unused)) const struct lfs_config cfg = {
        .context        = &bd,
        .read           = lfs_testbd_read,
        .prog           = lfs_testbd_prog,
        .erase          = lfs_testbd_erase,
        .sync           = lfs_testbd_sync,
        .read_size      = LFS_READ_SIZE,
        .prog_size      = LFS_PROG_SIZE,
        .block_size     = LFS_BLOCK_SIZE,
        .block_count    = LFS_BLOCK_COUNT,
        .block_cycles   = LFS_BLOCK_CYCLES,
        .cache_size     = LFS_CACHE_SIZE,
        .lookahead_size = LFS_LOOKAHEAD_SIZE,
    };

    __attribute__((unused)) const struct lfs_testbd_config bdcfg = {
        .erase_value        = LFS_ERASE_VALUE,
        .erase_cycles       = LFS_ERASE_CYCLES,
        .badblock_behavior  = LFS_BADBLOCK_BEHAVIOR,
        .power_cycles       = lfs_testbd_cycles,
    };

    lfs_testbd_createcfg(&cfg, lfs_testbd_path, &bdcfg) => 0;
"""
EPILOGUE = """
    // epilogue
    lfs_testbd_destroy(&cfg) => 0;
"""
PASS = '\033[32m✓\033[0m'
FAIL = '\033[31m✗\033[0m'

class TestFailure(Exception):
    def __init__(self, case, returncode=None, stdout=None, assert_=None):
        self.case = case
        self.returncode = returncode
        self.stdout = stdout
        self.assert_ = assert_

class TestCase:
    def __init__(self, config, filter=filter,
            suite=None, caseno=None, lineno=None, **_):
        self.config = config
        self.filter = filter
        self.suite = suite
        self.caseno = caseno
        self.lineno = lineno

        self.code = config['code']
        self.code_lineno = config['code_lineno']
        self.defines = config.get('define', {})
        self.if_ = config.get('if', None)
        self.in_ = config.get('in', None)

    def __str__(self):
        if hasattr(self, 'permno'):
            if any(k not in self.case.defines for k in self.defines):
                return '%s#%d#%d (%s)' % (
                    self.suite.name, self.caseno, self.permno, ', '.join(
                        '%s=%s' % (k, v) for k, v in self.defines.items()
                        if k not in self.case.defines))
            else:
                return '%s#%d#%d' % (
                    self.suite.name, self.caseno, self.permno)
        else:
            return '%s#%d' % (
                self.suite.name, self.caseno)

    def permute(self, class_=None, defines={}, permno=None, **_):
        ncase = (class_ or type(self))(self.config)
        for k, v in self.__dict__.items():
            setattr(ncase, k, v)
        ncase.case = self
        ncase.perms = [ncase]
        ncase.permno = permno
        ncase.defines = defines
        return ncase

    def build(self, f, **_):
        # prologue
        for k, v in sorted(self.defines.items()):
            if k not in self.suite.defines:
                f.write('#define %s %s\n' % (k, v))

        f.write('void test_case%d(%s) {' % (self.caseno, ','.join(
            '\n'+8*' '+'__attribute__((unused)) intmax_t %s' % k
            for k in sorted(self.perms[0].defines)
            if k not in self.defines)))

        f.write(PROLOGUE)
        f.write('\n')
        f.write(4*' '+'// test case %d\n' % self.caseno)
        f.write(4*' '+'#line %d "%s"\n' % (self.code_lineno, self.suite.path))

        # test case goes here
        f.write(self.code)

        # epilogue
        f.write(EPILOGUE)
        f.write('}\n')

        for k, v in sorted(self.defines.items()):
            if k not in self.suite.defines:
                f.write('#undef %s\n' % k)

    def shouldtest(self, **args):
        if (self.filter is not None and
                len(self.filter) >= 1 and
                self.filter[0] != self.caseno):
            return False
        elif (self.filter is not None and
                len(self.filter) >= 2 and
                self.filter[1] != self.permno):
            return False
        elif args.get('no_internal', False) and self.in_ is not None:
            return False
        elif self.if_ is not None:
            if_ = self.if_
            while True:
                for k, v in sorted(self.defines.items(),
                        key=lambda x: len(x[0]), reverse=True):
                    if k in if_:
                        if_ = if_.replace(k, '(%s)' % v)
                        break
                else:
                    break
            if_ = (
                re.sub('(\&\&|\?)', ' and ',
                re.sub('(\|\||:)', ' or ',
                re.sub('!(?!=)', ' not ', if_))))
            return eval(if_)
        else:
            return True

    def test(self, exec=[], persist=False, cycles=None,
            gdb=False, failure=None, disk=None, **args):
        # build command
        cmd = exec + ['./%s.test' % self.suite.path,
            repr(self.caseno), repr(self.permno)]

        # persist disk or keep in RAM for speed?
        if persist:
            if not disk:
                disk = self.suite.path + '.disk'
            if persist != 'noerase':
                try:
                    with open(disk, 'w') as f:
                        f.truncate(0)
                    if args.get('verbose', False):
                        print('truncate --size=0', disk)
                except FileNotFoundError:
                    pass

            cmd.append(disk)

        # simulate power-loss after n cycles?
        if cycles:
            cmd.append(str(cycles))

        # failed? drop into debugger?
        if gdb and failure:
            ncmd = ['gdb']
            if gdb == 'assert':
                ncmd.extend(['-ex', 'r'])
                if failure.assert_:
                    ncmd.extend(['-ex', 'up 2'])
            elif gdb == 'main':
                ncmd.extend([
                    '-ex', 'b %s:%d' % (self.suite.path, self.code_lineno),
                    '-ex', 'r'])
            ncmd.extend(['--args'] + cmd)

            if args.get('verbose', False):
                print(' '.join(shlex.quote(c) for c in ncmd))
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            sys.exit(sp.call(ncmd))

        # run test case!
        mpty, spty = pty.openpty()
        if args.get('verbose', False):
            print(' '.join(shlex.quote(c) for c in cmd))
        proc = sp.Popen(cmd, stdout=spty, stderr=spty)
        os.close(spty)
        mpty = os.fdopen(mpty, 'r', 1)
        stdout = []
        assert_ = None
        try:
            while True:
                try:
                    line = mpty.readline()
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                stdout.append(line)
                if args.get('verbose', False):
                    sys.stdout.write(line)
                # intercept asserts
                m = re.match(
                    '^{0}([^:]+):(\d+):(?:\d+:)?{0}{1}:{0}(.*)$'
                    .format('(?:\033\[[\d;]*.| )*', 'assert'),
                    line)
                if m and assert_ is None:
                    try:
                        with open(m.group(1)) as f:
                            lineno = int(m.group(2))
                            line = (next(it.islice(f, lineno-1, None))
                                .strip('\n'))
                        assert_ = {
                            'path': m.group(1),
                            'line': line,
                            'lineno': lineno,
                            'message': m.group(3)}
                    except:
                        pass
        except KeyboardInterrupt:
            raise TestFailure(self, 1, stdout, None)
        proc.wait()

        # did we pass?
        if proc.returncode != 0:
            raise TestFailure(self, proc.returncode, stdout, assert_)
        else:
            return PASS

class ValgrindTestCase(TestCase):
    def __init__(self, config, **args):
        self.leaky = config.get('leaky', False)
        super().__init__(config, **args)

    def shouldtest(self, **args):
        return not self.leaky and super().shouldtest(**args)

    def test(self, exec=[], **args):
        verbose = args.get('verbose', False)
        uninit = (self.defines.get('LFS_ERASE_VALUE', None) == -1)
        exec = [
            'valgrind',
            '--leak-check=full',
            ] + (['--undef-value-errors=no'] if uninit else []) + [
            ] + (['--track-origins=yes'] if not uninit else []) + [
            '--error-exitcode=4',
            '--error-limit=no',
            ] + (['--num-callers=1'] if not verbose else []) + [
            '-q'] + exec
        return super().test(exec=exec, **args)

class ReentrantTestCase(TestCase):
    def __init__(self, config, **args):
        self.reentrant = config.get('reentrant', False)
        super().__init__(config, **args)

    def shouldtest(self, **args):
        return self.reentrant and super().shouldtest(**args)

    def test(self, persist=False, gdb=False, failure=None, **args):
        for cycles in it.count(1):
            # clear disk first?
            if cycles == 1 and persist != 'noerase':
                persist = 'erase'
            else:
                persist = 'noerase'

            # exact cycle we should drop into debugger?
            if gdb and failure and failure.cycleno == cycles:
                return super().test(gdb=gdb, persist=persist, cycles=cycles,
                    failure=failure, **args)

            # run tests, but kill the program after prog/erase has
            # been hit n cycles. We exit with a special return code if the
            # program has not finished, since this isn't a test failure.
            try:
                return super().test(persist=persist, cycles=cycles, **args)
            except TestFailure as nfailure:
                if nfailure.returncode == 33:
                    continue
                else:
                    nfailure.cycleno = cycles
                    raise

class TestSuite:
    def __init__(self, path, classes=[TestCase], defines={},
            filter=None, **args):
        self.name = os.path.basename(path)
        if self.name.endswith('.toml'):
            self.name = self.name[:-len('.toml')]
        if args.get('build_dir'):
            self.toml = path
            self.path = args['build_dir'] + '/' + path
        else:
            self.toml = path
            self.path = path
        self.classes = classes
        self.defines = defines.copy()
        self.filter = filter

        with open(self.toml) as f:
            # load tests
            config = toml.load(f)

            # find line numbers
            f.seek(0)
            linenos = []
            code_linenos = []
            for i, line in enumerate(f):
                if re.match(r'\[\[\s*case\s*\]\]', line):
                    linenos.append(i+1)
                if re.match(r'code\s*=\s*(\'\'\'|""")', line):
                    code_linenos.append(i+2)

            code_linenos.reverse()

        # grab global config
        for k, v in config.get('define', {}).items():
            if k not in self.defines:
                self.defines[k] = v
        self.code = config.get('code', None)
        if self.code is not None:
            self.code_lineno = code_linenos.pop()

        # create initial test cases
        self.cases = []
        for i, (case, lineno) in enumerate(zip(config['case'], linenos)):
            # code lineno?
            if 'code' in case:
                case['code_lineno'] = code_linenos.pop()
            # merge conditions if necessary
            if 'if' in config and 'if' in case:
                case['if'] = '(%s) && (%s)' % (config['if'], case['if'])
            elif 'if' in config:
                case['if'] = config['if']
            # initialize test case
            self.cases.append(TestCase(case, filter=filter,
                suite=self, caseno=i+1, lineno=lineno, **args))

    def __str__(self):
        return self.name

    def __lt__(self, other):
        return self.name < other.name

    def permute(self, **args):
        for case in self.cases:
            # lets find all parameterized definitions, in one of [args.D,
            # suite.defines, case.defines, DEFINES]. Note that each of these
            # can be either a dict of defines, or a list of dicts, expressing
            # an initial set of permutations.
            pending = [{}]
            for inits in [self.defines, case.defines, DEFINES]:
                if not isinstance(inits, list):
                    inits = [inits]

                npending = []
                for init, pinit in it.product(inits, pending):
                    ninit = pinit.copy()
                    for k, v in init.items():
                        if k not in ninit:
                            try:
                                ninit[k] = eval(v)
                            except:
                                ninit[k] = v
                    npending.append(ninit)

                pending = npending

            # expand permutations
            pending = list(reversed(pending))
            expanded = []
            while pending:
                perm = pending.pop()
                for k, v in sorted(perm.items()):
                    if not isinstance(v, str) and isinstance(v, abc.Iterable):
                        for nv in reversed(v):
                            nperm = perm.copy()
                            nperm[k] = nv
                            pending.append(nperm)
                        break
                else:
                    expanded.append(perm)

            # generate permutations
            case.perms = []
            for i, (class_, defines) in enumerate(
                    it.product(self.classes, expanded)):
                case.perms.append(case.permute(
                    class_, defines, permno=i+1, **args))

            # also track non-unique defines
            case.defines = {}
            for k, v in case.perms[0].defines.items():
                if all(perm.defines[k] == v for perm in case.perms):
                    case.defines[k] = v

        # track all perms and non-unique defines
        self.perms = []
        for case in self.cases:
            self.perms.extend(case.perms)

        self.defines = {}
        for k, v in self.perms[0].defines.items():
            if all(perm.defines.get(k, None) == v for perm in self.perms):
                self.defines[k] = v

        return self.perms

    def build(self, **args):
        # build test files
        tf = open(self.path + '.test.tc', 'w')
        tf.write(GLOBALS)
        if self.code is not None:
            tf.write('#line %d "%s"\n' % (self.code_lineno, self.path))
            tf.write(self.code)

        tfs = {None: tf}
        for case in self.cases:
            if case.in_ not in tfs:
                tfs[case.in_] = open(self.path+'.'+
                    re.sub('(\.c)?$', '.tc', case.in_.replace('/', '.')), 'w')
                tfs[case.in_].write('#line 1 "%s"\n' % case.in_)
                with open(case.in_) as f:
                    for line in f:
                        tfs[case.in_].write(line)
                tfs[case.in_].write('\n')
                tfs[case.in_].write(GLOBALS)

            tfs[case.in_].write('\n')
            case.build(tfs[case.in_], **args)

        tf.write('\n')
        tf.write('const char *lfs_testbd_path;\n')
        tf.write('uint32_t lfs_testbd_cycles;\n')
        tf.write('int main(int argc, char **argv) {\n')
        tf.write(4*' '+'int case_         = (argc > 1) ? atoi(argv[1]) : 0;\n')
        tf.write(4*' '+'int perm          = (argc > 2) ? atoi(argv[2]) : 0;\n')
        tf.write(4*' '+'lfs_testbd_path   = (argc > 3) ? argv[3] : NULL;\n')
        tf.write(4*' '+'lfs_testbd_cycles = (argc > 4) ? atoi(argv[4]) : 0;\n')
        for perm in self.perms:
            # test declaration
            tf.write(4*' '+'extern void test_case%d(%s);\n' % (
                perm.caseno, ', '.join(
                    'intmax_t %s' % k for k in sorted(perm.defines)
                    if k not in perm.case.defines)))
            # test call
            tf.write(4*' '+
                'if (argc < 3 || (case_ == %d && perm == %d)) {'
                ' test_case%d(%s); '
                '}\n' % (perm.caseno, perm.permno, perm.caseno, ', '.join(
                    str(v) for k, v in sorted(perm.defines.items())
                    if k not in perm.case.defines)))
        tf.write('}\n')

        for tf in tfs.values():
            tf.close()

        # write makefiles
        with open(self.path + '.mk', 'w') as mk:
            mk.write(RULES.replace(4*' ', '\t') % dict(path=self.path))
            mk.write('\n')

            # add coverage hooks?
            if args.get('coverage', False):
                mk.write(COVERAGE_RULES.replace(4*' ', '\t') % dict(
                    path=self.path))
                mk.write('\n')

            # add truely global defines globally
            for k, v in sorted(self.defines.items()):
                mk.write('%s.test: override CFLAGS += -D%s=%r\n'
                    % (self.path, k, v))

            for path in tfs:
                if path is None:
                    mk.write('%s: %s | %s\n' % (
                        self.path+'.test.c',
                        self.toml,
                        self.path+'.test.tc'))
                else:
                    mk.write('%s: %s %s | %s\n' % (
                        self.path+'.'+path.replace('/', '.'),
                        self.toml,
                        path,
                        self.path+'.'+re.sub('(\.c)?$', '.tc',
                            path.replace('/', '.'))))
                mk.write('\t./scripts/explode_asserts.py $| -o $@\n')

        self.makefile = self.path + '.mk'
        self.target = self.path + '.test'
        return self.makefile, self.target

    def test(self, **args):
        # run test suite!
        if not args.get('verbose', True):
            sys.stdout.write(self.name + ' ')
            sys.stdout.flush()
        for perm in self.perms:
            if not perm.shouldtest(**args):
                continue

            try:
                result = perm.test(**args)
            except TestFailure as failure:
                perm.result = failure
                if not args.get('verbose', True):
                    sys.stdout.write(FAIL)
                    sys.stdout.flush()
                if not args.get('keep_going', False):
                    if not args.get('verbose', True):
                        sys.stdout.write('\n')
                    raise
            else:
                perm.result = PASS
                if not args.get('verbose', True):
                    sys.stdout.write(PASS)
                    sys.stdout.flush()

        if not args.get('verbose', True):
            sys.stdout.write('\n')

def main(**args):
    # figure out explicit defines
    defines = {}
    for define in args['D']:
        k, v, *_ = define.split('=', 2) + ['']
        defines[k] = v

    # and what class of TestCase to run
    classes = []
    if args.get('normal', False):
        classes.append(TestCase)
    if args.get('reentrant', False):
        classes.append(ReentrantTestCase)
    if args.get('valgrind', False):
        classes.append(ValgrindTestCase)
    if not classes:
        classes = [TestCase]

    suites = []
    for testpath in args['test_paths']:
        # optionally specified test case/perm
        testpath, *filter = testpath.split('#')
        filter = [int(f) for f in filter]

        # figure out the suite's toml file
        if os.path.isdir(testpath):
            testpath = testpath + '/*.toml'
        elif os.path.isfile(testpath):
            testpath = testpath
        elif testpath.endswith('.toml'):
            testpath = TEST_PATHS + '/' + testpath
        else:
            testpath = TEST_PATHS + '/' + testpath + '.toml'

        # find tests
        for path in glob.glob(testpath):
            suites.append(TestSuite(path, classes, defines, filter, **args))

    # sort for reproducability
    suites = sorted(suites)

    # generate permutations
    for suite in suites:
        suite.permute(**args)

    # build tests in parallel
    print('====== building ======')
    makefiles = []
    targets = []
    for suite in suites:
        makefile, target = suite.build(**args)
        makefiles.append(makefile)
        targets.append(target)

    cmd = (['make', '-f', 'Makefile'] +
        list(it.chain.from_iterable(['-f', m] for m in makefiles)) +
        [target for target in targets])
    mpty, spty = pty.openpty()
    if args.get('verbose', False):
        print(' '.join(shlex.quote(c) for c in cmd))
    proc = sp.Popen(cmd, stdout=spty, stderr=spty)
    os.close(spty)
    mpty = os.fdopen(mpty, 'r', 1)
    stdout = []
    while True:
        try:
            line = mpty.readline()
        except OSError as e:
            if e.errno == errno.EIO:
                break
            raise
        stdout.append(line)
        if args.get('verbose', False):
            sys.stdout.write(line)
        # intercept warnings
        m = re.match(
            '^{0}([^:]+):(\d+):(?:\d+:)?{0}{1}:{0}(.*)$'
            .format('(?:\033\[[\d;]*.| )*', 'warning'),
            line)
        if m and not args.get('verbose', False):
            try:
                with open(m.group(1)) as f:
                    lineno = int(m.group(2))
                    line = next(it.islice(f, lineno-1, None)).strip('\n')
                sys.stdout.write(
                    "\033[01m{path}:{lineno}:\033[01;35mwarning:\033[m "
                    "{message}\n{line}\n\n".format(
                        path=m.group(1), line=line, lineno=lineno,
                        message=m.group(3)))
            except:
                pass
    proc.wait()

    if proc.returncode != 0:
        if not args.get('verbose', False):
            for line in stdout:
                sys.stdout.write(line)
        sys.exit(-1)

    print('built %d test suites, %d test cases, %d permutations' % (
        len(suites),
        sum(len(suite.cases) for suite in suites),
        sum(len(suite.perms) for suite in suites)))

    total = 0
    for suite in suites:
        for perm in suite.perms:
            total += perm.shouldtest(**args)
    if total != sum(len(suite.perms) for suite in suites):
        print('total down to %d permutations' % total)

    # only requested to build?
    if args.get('build', False):
        return 0

    print('====== testing ======')
    try:
        for suite in suites:
            suite.test(**args)
    except TestFailure:
        pass

    print('====== results ======')
    passed = 0
    failed = 0
    for suite in suites:
        for perm in suite.perms:
            if not hasattr(perm, 'result'):
                continue

            if perm.result == PASS:
                passed += 1
            else:
                sys.stdout.write(
                    "\033[01m{path}:{lineno}:\033[01;31mfailure:\033[m "
                    "{perm} failed\n".format(
                        perm=perm, path=perm.suite.path, lineno=perm.lineno,
                        returncode=perm.result.returncode or 0))
                if perm.result.stdout:
                    if perm.result.assert_:
                        stdout = perm.result.stdout[:-1]
                    else:
                        stdout = perm.result.stdout
                    for line in stdout[-5:]:
                        sys.stdout.write(line)
                if perm.result.assert_:
                    sys.stdout.write(
                        "\033[01m{path}:{lineno}:\033[01;31massert:\033[m "
                        "{message}\n{line}\n".format(
                            **perm.result.assert_))
                sys.stdout.write('\n')
                failed += 1

    if args.get('coverage', False):
        # collect coverage info
        cmd = (['make', '-f', 'Makefile'] +
            list(it.chain.from_iterable(['-f', m] for m in makefiles)) +
            [re.sub('\.test$', '.cumul.info', target) for target in targets])
        if args.get('verbose', False):
            print(' '.join(shlex.quote(c) for c in cmd))
        proc = sp.Popen(cmd,
            stdout=sp.DEVNULL if not args.get('verbose', False) else None)
        proc.wait()
        if proc.returncode != 0:
            sys.exit(-1)

    if args.get('gdb', False):
        failure = None
        for suite in suites:
            for perm in suite.perms:
                if getattr(perm, 'result', PASS) != PASS:
                    failure = perm.result
        if failure is not None:
            print('======= gdb ======')
            # drop into gdb
            failure.case.test(failure=failure, **args)
            sys.exit(0)

    print('tests passed %d/%d (%.2f%%)' % (passed, total,
        100*(passed/total if total else 1.0)))
    print('tests failed %d/%d (%.2f%%)' % (failed, total,
        100*(failed/total if total else 1.0)))
    return 1 if failed > 0 else 0

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Run parameterized tests in various configurations.")
    parser.add_argument('test_paths', nargs='*', default=[TEST_PATHS],
        help="Description of test(s) to run. By default, this is all tests \
            found in the \"{0}\" directory. Here, you can specify a different \
            directory of tests, a specific file, a suite by name, and even a \
            specific test case by adding brackets. For example \
            \"test_dirs[0]\" or \"{0}/test_dirs.toml[0]\".".format(TEST_PATHS))
    parser.add_argument('-D', action='append', default=[],
        help="Overriding parameter definitions.")
    parser.add_argument('-v', '--verbose', action='store_true',
        help="Output everything that is happening.")
    parser.add_argument('-k', '--keep-going', action='store_true',
        help="Run all tests instead of stopping on first error. Useful for CI.")
    parser.add_argument('-p', '--persist', choices=['erase', 'noerase'],
        nargs='?', const='erase',
        help="Store disk image in a file.")
    parser.add_argument('-b', '--build', action='store_true',
        help="Only build the tests, do not execute.")
    parser.add_argument('-g', '--gdb', choices=['init', 'main', 'assert'],
        nargs='?', const='assert',
        help="Drop into gdb on test failure.")
    parser.add_argument('--no-internal', action='store_true',
        help="Don't run tests that require internal knowledge.")
    parser.add_argument('-n', '--normal', action='store_true',
        help="Run tests normally.")
    parser.add_argument('-r', '--reentrant', action='store_true',
        help="Run reentrant tests with simulated power-loss.")
    parser.add_argument('--valgrind', action='store_true',
        help="Run non-leaky tests under valgrind to check for memory leaks.")
    parser.add_argument('--exec', default=[], type=lambda e: e.split(),
        help="Run tests with another executable prefixed on the command line.")
    parser.add_argument('--disk',
        help="Specify a file to use for persistent/reentrant tests.")
    parser.add_argument('--coverage', action='store_true',
        help="Collect coverage information during testing. This uses lcov/gcov \
            to accumulate coverage information into *.info files. Note \
            coverage is not reset between runs, allowing multiple runs to \
            contribute to coverage.")
    parser.add_argument('--build-dir',
        help="Build relative to the specified directory instead of the \
            current directory.")

    sys.exit(main(**vars(parser.parse_args())))
