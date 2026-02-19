# FND-001: SSH root login enabled with password authentication
Severity: CRITICAL
Verified: YES
Source-Model: both

## Runtime Evidence

Confirmed via runtime checks:

```
$ sudo sshd -T | grep -E 'permitrootlogin|passwordauthentication'
permitrootlogin yes
passwordauthentication yes

$ ss -tlnp | grep :22
LISTEN  0  128  0.0.0.0:22  0.0.0.0:*  users:(("sshd",pid=1042,fd=3))
LISTEN  0  128  [::]:22     [::]:*     users:(("sshd",pid=1042,fd=3))

$ sudo journalctl -u sshd --since "7 days ago" | grep "Failed password" | wc -l
1847
```

1,847 failed password attempts in the last 7 days confirms active brute-force activity.

## Analysis

This is a CRITICAL finding because:
1. Root login is permitted directly via SSH
2. Password authentication allows brute-force attacks
3. SSH is bound to all interfaces on the default port
4. Auth logs show active exploitation attempts (1,847 failures/week)

## Fix

```bash
# /etc/ssh/sshd_config
PermitRootLogin no
PasswordAuthentication no

# Restart
sudo systemctl restart sshd
```

Ensure you have a non-root user with sudo access and working SSH key authentication
before applying this fix. Test with a second SSH session before closing your current one.

## Rollback

Revert the two lines in sshd_config and restart sshd. If locked out, use console access.
