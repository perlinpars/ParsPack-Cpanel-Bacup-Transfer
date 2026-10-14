#!/usr/bin/perl
use strict;
use warnings;
use Fcntl qw(O_WRONLY O_APPEND O_CREAT O_NOFOLLOW O_EXCL F_GETFD F_SETFD FD_CLOEXEC);
use IO::Handle;
use IO::Select;
use POSIX qw(strftime setsid);

# Capture the destination password, then drop the inherited environment.
# PARSPACK_TEST_ROOT is only honored for a non-root test run. cPanel runs this as root.
my $given_password = $ENV{PASSWORD};
my $test_root = ($> != 0) ? $ENV{PARSPACK_TEST_ROOT} : undef;
my $home = ($> == 0) ? '/root' : (defined $ENV{HOME} ? $ENV{HOME} : '/tmp');
%ENV = (
    PATH   => '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    LC_ALL => 'C',
    HOME   => $home,
);
umask 0077;

my $ROOT = '/root/parspack-backup';
if (defined $test_root && $test_root =~ m{\A/[^\r\n\0]+\z} && !-l $test_root && -d $test_root) {
    $ROOT = $test_root;
}
my $PY = "$ROOT/lib/parspack_config.py";
my $ENGINE = "$ROOT/bin/engine-upload.sh";
my $REMOTE_OPS = "$ROOT/bin/remote-ops.sh";
my $TRACE_LOG = "$ROOT/logs/transport-trace.log";
my $TRACE = 0;

sub trace {
    return unless $TRACE;
    my ($message) = @_;
    $message =~ s/[^\x20-\x7e]/?/g;
    sysopen(my $fh, $TRACE_LOG, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0600) or return;
    print $fh strftime('%Y-%m-%d %H:%M:%S', localtime) . " pid=$$ $message\n";
    close $fh;
}

$SIG{__DIE__} = sub {
    my ($error) = @_;
    return if $^S;
    chomp $error;
    trace("DIE: $error");
    print STDERR "$error\n";
    exit 1;
};

sub root_owned {
    return 1 if $> != 0;
    for my $path (@_) {
        my @st = lstat($path) or die "PARSPACK: cannot stat $path\n";
        die "PARSPACK: unsafe ownership or permissions on $path\n" if -l _ || $st[4] != 0 || ($st[2] & 022);
    }
    return 1;
}

sub python_bin {
    return '/usr/bin/python3' if -x '/usr/bin/python3';
    return '/usr/local/bin/python3' if -x '/usr/local/bin/python3';
    die "python3 not found\n";
}

sub secure_eq {
    my ($left, $right) = @_;
    $left  = '' if !defined $left;
    $right = '' if !defined $right;
    my $max = length($left) > length($right) ? length($left) : length($right);
    my $diff = length($left) ^ length($right);
    for (my $i = 0; $i < $max; $i++) {
        my $a = $i < length($left)  ? ord(substr($left,  $i, 1)) : 0;
        my $b = $i < length($right) ? ord(substr($right, $i, 1)) : 0;
        $diff |= $a ^ $b;
    }
    return $diff == 0;
}

sub py_stdout {
    my (@args) = @_;
    my $pid = open(my $fh, '-|', python_bin(), $PY, @args);
    die "PARSPACK helper failed to start\n" if !defined $pid;
    my $output = do { local $/; <$fh> };
    close $fh;
    die "PARSPACK configuration error\n" if $? != 0;
    $output = '' if !defined $output;
    # The slurp above leaves $/ undefined, and chomp removes nothing in that
    # state. Restore the normal separator before removing one trailing newline.
    chomp $output;
    die "PARSPACK helper returned an invalid value\n" if $output =~ /[\r\n\0]/;
    return $output;
}

sub cfg {
    my ($key) = @_;
    my $value = eval { py_stdout('get', $key) };
    return defined $value ? $value : '';
}

sub authenticated {
    if (cfg('TRANSPORT_AUTH_DISABLED') eq '1') {
        trace('AUTH disabled by TRANSPORT_AUTH_DISABLED=1');
        print STDERR "PARSPACK: WARNING transport authentication is disabled\n";
        undef $given_password;
        return 1;
    }
    my $expected = py_stdout('get', 'TRANSPORT_PASSWORD');
    my $supplied = defined $given_password && length $given_password ? 'yes' : 'no';
    trace("AUTH password_supplied=$supplied");
    my $ok = secure_eq(defined $given_password ? $given_password : '', $expected);
    undef $expected;
    undef $given_password;
    return $ok;
}

sub usage {
    print STDERR "PARSPACK custom backup transport: cmd local_dir [args...]\n";
    exit 1;
}

sub note_mirror {
    my ($message) = @_;
    $message =~ s/[^\x20-\x7e]/?/g;
    mkdir "$ROOT/logs", 0700;
    if (sysopen(my $fh, "$ROOT/logs/send-system.log", O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0600)) {
        print $fh strftime('%Y-%m-%dT%H:%M:%S', localtime) . " [ERROR] $message\n";
        close $fh;
    }
    print STDERR "PARSPACK: $message\n";
}

sub backup_day {
    my ($local) = @_;
    return '' if !defined $local || $local =~ m{[\r\n\0]};
    return '' if $local =~ m{validate\.tmp-};
    return '' if $local !~ m{/(\d{4}-\d{2}-\d{2})/accounts/[A-Za-z0-9._+-]+\.tar\.gz\z};
    return $1;
}

sub write_system_pending {
    my ($date) = @_;
    return 0 if $date !~ m{\A\d{4}-\d{2}-\d{2}\z};
    my $dir = "$ROOT/state";
    return 0 if -l $dir;
    mkdir $dir, 0700 if !-d $dir;
    return 0 if -l $dir || !-d $dir;
    my @dir_stat = lstat($dir);
    return 0 if !@dir_stat || $dir_stat[4] != 0 || ($dir_stat[2] & 022);
    my $final = "$dir/system-pending-$date";
    return 0 if -l $final;
    my $tmp = "$dir/.system-pending-$date.$$.tmp";
    sysopen(my $fh, $tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600) or return 0;
    my $ok = print $fh "date=$date\n" ? 1 : 0;
    # flush and sync return numeric 0 on success. The POD calls that
    # "0 but true". -1 and undef are failures, so a boolean check is wrong.
    # The state directory is not synced: this process never opens it.
    if ($ok) {
        my $flushed = $fh->flush;
        $ok = defined $flushed && $flushed == 0;
    }
    if ($ok) {
        my $synced = $fh->sync;
        if (!defined $synced || $synced != 0) {
            $ok = 0;
        }
    }
    if (!close $fh) {
        $ok = 0;
    }
    if (!$ok) {
        unlink $tmp;
        return 0;
    }
    if (!rename($tmp, $final)) {
        unlink $tmp;
        return 0;
    }
    if (!chmod 0600, $final) {
        unlink $final;
        return 0;
    }
    my @st = lstat($final);
    return @st && !-l $final && $st[4] == 0 && (($st[2] & 0777) == 0600);
}

sub spawn_system_mirror {
    my ($local, $remote) = @_;
    my $date = backup_day($local);
    return 'skip' if $date eq '';
    return 'skip' if !defined $remote || $remote =~ m{[\r\n\0]};
    write_system_pending($date) or return 'marker-failed';
    my $pid = fork();
    if (!defined $pid) {
        note_mirror("system mirror fork failed; marker kept for $date");
        return 'spawn-failed';
    }
    if ($pid == 0) {
        my $sid = setsid();
        exit 2 if !defined $sid || $sid < 0;
        pipe(my $read_fh, my $write_fh) or exit 3;
        my $flags = fcntl($write_fh, F_GETFD, 0);
        fcntl($write_fh, F_SETFD, ($flags || 0) | FD_CLOEXEC);
        my $pid2 = fork();
        exit 3 if !defined $pid2;
        if ($pid2 == 0) {
            close $read_fh;
            open STDIN, '<', '/dev/null' or do { print $write_fh "stdin\n"; exit 1 };
            mkdir "$ROOT/logs", 0700;
            open STDOUT, '>>', "$ROOT/logs/send-system.log" or do { print $write_fh "stdout\n"; exit 1 };
            open STDERR, '>&', \*STDOUT or do { print $write_fh "stderr\n"; exit 1 };
            exec '/bin/bash', "$ROOT/bin/send-system.sh", '--run', $local, $remote
                or do {
                    print $write_fh "exec failed\n";
                    exit 1;
                };
        }
        close $write_fh;
        my $err = '';
        my $sel = IO::Select->new($read_fh);
        if ($sel->can_read(2)) {
            $err = do { local $/; <$read_fh> };
            $err = '' if !defined $err;
        }
        close $read_fh;
        exit 4 if $err ne '';
        exit 0;
    }
    waitpid($pid, 0);
    if ($? != 0) {
        note_mirror("system mirror did not detach for $date; the account file is already verified and the pending marker was kept");
        return 'spawn-failed';
    }
    return 'spawned';
}

sub run_ok {
    my (@cmd) = @_;
    trace('EXEC ' . join(' ', map { defined $_ ? $_ : '' } @cmd[0 .. 1]));
    my $status = system(@cmd);
    trace("EXEC status=$status");
    if ($status == -1 || ($status & 127)) {
        exit 1;
    }
    exit($status >> 8);
}

sub set_local_dir {
    my ($dir) = @_;
    delete $ENV{PARSPACK_LOCAL_DIR};
    return if !defined $dir || $dir eq '/' || $dir !~ m{\A/[^\r\n\0]*\z};
    return if -l $dir || !-d $dir;
    $ENV{PARSPACK_LOCAL_DIR} = $dir;
}

sub cwd_args {
    my ($dir) = @_;
    return () if !defined $dir || $dir eq '/' || $dir !~ m{\A/[^\r\n\0]*\z};
    return () if -l $dir || !-d $dir;
    return ('--cwd', $dir);
}

sub composed_remote {
    my ($path, $dated, $hint, $dir) = @_;
    my $cwd = defined $dir ? $dir : '';
    my @cmd = ('remote-compose', '--cwd', $cwd);
    if ($dated) {
        push @cmd, '--dated', (defined $hint ? $hint : '');
    }
    push @cmd, '--', (defined $path ? $path : '');
    return py_stdout(@cmd);
}

root_owned($ROOT, "$ROOT/bin", "$ROOT/lib", "$ROOT/config", $PY, $ENGINE, $REMOTE_OPS, "$ROOT/lib/parspack-common.sh", "$ROOT/config/parspack.conf");
-x $ENGINE or die "PARSPACK upload engine missing\n";
-x $REMOTE_OPS or die "PARSPACK remote-ops helper missing\n";
$TRACE = cfg('TRANSPORT_TRACE') eq '1' ? 1 : 0;

@ARGV >= 2 or usage();
my ($cmd, $local_dir, @args) = @ARGV;
trace("CMD=$cmd argc=" . scalar(@ARGV));
trace('ARGV=' . join('|', @ARGV)) if $TRACE;
set_local_dir($local_dir);

authenticated() or do { trace('AUTH FAILED'); die "PARSPACK transport authentication failed\n"; };
trace('AUTH OK');

if ($cmd eq 'put') {
    # cPanel calls: put <local_dir> <local_file> <remote_file> <host> <user>
    @args >= 2 or usage();
    my ($local_file, $dest_file) = @args[0, 1];
    die "remote file name is required\n" if !defined $dest_file || $dest_file eq '';
    my $canonical = py_stdout('check-local', 'read', cwd_args($local_dir), '--', $local_file);
    # The date directory comes from chdir's previous STDOUT, which cPanel passes
    # back as $local_dir, or from the backup path itself. It is not process state.
    my $remote = composed_remote($dest_file, 1, $canonical, $local_dir);
    trace('EXEC ' . $ENGINE);
    my $status = system($ENGINE, $canonical, $remote);
    trace("EXEC status=$status");
    my $code = 1;
    if ($status != -1 && ($status & 127) == 0) {
        $code = $status >> 8;
    }
    if ($code == 0) {
        my $day = backup_day($canonical);
        my $needed = '1';
        if ($day ne '') {
            my $flag = eval { py_stdout('mirror-needed', '--', $day) };
            $needed = defined $flag ? $flag : '1';
        }
        if ($needed eq '0') {
            note_mirror("system mirror skipped for $day; day is already partial or complete");
        } else {
            my $mirror = spawn_system_mirror($canonical, $remote);
            if ($mirror eq 'marker-failed') {
                note_mirror('system pending marker could not be written; put failed closed');
                exit 1;
            }
        }
    }
    exit $code;
}

if ($cmd eq 'ls' || $cmd eq 'mkdir') {
    my $path = defined $args[0] ? $args[0] : '';
    if (!defined $path || $path eq '' || $path eq '/') {
        run_ok($REMOTE_OPS, $cmd, '');
    }
    run_ok($REMOTE_OPS, $cmd, composed_remote($path, 0, '', $local_dir));
}

if ($cmd eq 'chdir') {
    # Each command is a new process. Print the remote directory cPanel must
    # pass as the directory argument of the next command.
    my $path = defined $args[0] ? $args[0] : '';
    my $shown = ($path eq '' || $path eq '/') ? '/' : composed_remote($path, 0, '', $local_dir);
    my $check = $shown eq '/' ? '/' : $shown;
    my $status = system($REMOTE_OPS, 'chdir', $check);
    if ($status == -1 || ($status & 127) || (($status >> 8) != 0)) {
        exit 1;
    }
    print $shown, "\n";
    exit 0;
}

if ($cmd eq 'get') {
    # cPanel calls: get <local_dir> <remote_file> <local_file> <host> <user>
    @args >= 2 or usage();
    my ($remote_file, $local_file) = @args[0, 1];
    die "local and remote names are required\n" if !length($remote_file // '') || !length($local_file // '');
    my $canonical = py_stdout('check-local', 'write', cwd_args($local_dir), '--', $local_file);
    run_ok($REMOTE_OPS, 'get', composed_remote($remote_file, 0, '', $local_dir), $canonical);
}

if ($cmd eq 'delete') {
    # cPanel calls: delete <local_dir> <remote_filename> <host> <user>
    # $local_dir was already consumed. $args[0] is the filename, not a directory.
    @args >= 1 or usage();
    my ($file, $host, $user) = @args[0, 1, 2];
    die "remote file name is required\n" if !defined $file || $file eq '';
    if ($file =~ /\Avalidate\.tmp-[0-9]+-[0-9]+\.txt\z/) {
        run_ok($REMOTE_OPS, 'delete-validation', $file);
    } else {
        run_ok($REMOTE_OPS, 'delete', composed_remote($file, 0, '', $local_dir));
    }
}

if ($cmd eq 'rmdir') {
    my $path = defined $args[0] ? $args[0] : '';
    die "remote path is required\n" if $path eq '';
    run_ok($REMOTE_OPS, 'rmdir', composed_remote($path, 0, '', $local_dir));
}

print STDERR "Unknown transport command\n";
exit 1;
