class Rsync < Formula
  desc "Utility that provides fast incremental file transfer"
  homepage "https://rsync.samba.org/"
  url "https://github.com/RsyncProject/rsync/releases/download/v3.5.0/rsync-3.5.0.tar.gz"
  mirror "https://rsync.samba.org/ftp/rsync/rsync-3.5.0.tar.gz"
  sha256 "c7ffd1ef653e99540f661e47cb00b7f9cad1ee6b972399b16f93d672656e0d33"
  license "GPL-3.0-or-later"

  depends_on "lz4"
  depends_on "openssl@3"
  depends_on "popt"
  depends_on "xxhash"
  depends_on "zstd"

  on_linux do
    depends_on "zlib-ng-compat"
  end

  # Fix Linux sandbox compatibility
  patch do
    url "https://github.com/RsyncProject/rsync/commit/56e37eae666c7b0a52d29e9d1fb27325aaa8acf0.patch?full_index=1"
    sha256 "3bb2d6096b7fc2dcf550af022f32c00cf8fa186ad84596c574ddac29c38e23fb"
    type :unofficial
    resolves "https://github.com/RsyncProject/rsync/pull/1052"
  end

  # macOS group-membership fix:
  # is_in_group uses getgrouplist(3) (full Directory Services membership)
  # instead of getgroups(2), which macOS truncates to kern.ngroups=16 and
  # silently drops groups that chown(2) would authorize -- that made
  # --chown/-g silently skip chgrp. No-op on non-Apple builds.
  #
  # Phase 2: --chown/--groupmap only marks regular
  # files -- directories keep the receiver's default group. Marked dirs made
  # macOS files created inside inherit the replica tag (dir group
  # inheritance), so dir-level marking is skipped at the receiver flist
  # mapping (FLAG_SKIP_GROUP on non-regular entries under groupmap).
  # Plain -g (no --chown/--groupmap) is unchanged.
  patch :DATA

  def install
    args = %W[
      --with-rsyncd-conf=#{etc}/rsyncd.conf
      --with-included-popt=no
      --with-included-zlib=no
      --with-rrsync=yes
      --enable-ipv6
    ]

    system "./configure", *args, *std_configure_args
    system "make"
    system "make", "install"
  end

  test do
    mkdir "a"
    mkdir "b"

    ["foo\n", "bar\n", "baz\n"].map.with_index do |s, i|
      (testpath/"a/#{i + 1}.txt").write s
    end

    system bin/"rsync", "-artv", testpath/"a/", testpath/"b/"

    (1..3).each do |i|
      assert_equal (testpath/"a/#{i}.txt").read, (testpath/"b/#{i}.txt").read
    end
  end
end
__END__
--- a/uidlist.c	2026-08-28 17:14:05
+++ b/uidlist.c	2026-08-28 17:14:21
@@ -203,11 +203,34 @@
 	if (gid == last_in && last_out >= 0)
 		return last_out;
 	if (ngroups < -1) {
-		if ((ngroups = getgroups(0, NULL)) < 0)
-			ngroups = 0;
-		gidset = new_array(GETGROUPS_T, ngroups+1);
-		if (ngroups > 0)
-			ngroups = getgroups(ngroups, gidset);
+#if defined(__APPLE__) && defined(HAVE_GETGROUPLIST)
+		/* macOS keeps only kern.ngroups (16) supplementary groups in
+		 * the process credentials, so getgroups(2) may silently
+		 * truncate the user's full Directory Services membership
+		 * (missing e.g. the group --chown/-g targets) while chown(2)
+		 * authorization consults the full list.  Use getgrouplist(3)
+		 * to query the same full membership so the pre-check matches
+		 * what the kernel actually allows. */
+		struct passwd *pw = getpwuid(getuid());
+		if (pw) {
+			int size = 32;
+			gidset = new_array(GETGROUPS_T, size+1);
+			while (getgrouplist(pw->pw_name, (int)pw->pw_gid,
+					    (int *)gidset, &size) < 0 && size < 4096) {
+				/* macOS does not update "size" on failure, so just grow. */
+				size *= 2;
+				gidset = realloc_array(gidset, GETGROUPS_T, size+1);
+			}
+			ngroups = size;
+		} else
+#endif
+		{
+			if ((ngroups = getgroups(0, NULL)) < 0)
+				ngroups = 0;
+			gidset = new_array(GETGROUPS_T, ngroups+1);
+			if (ngroups > 0)
+				ngroups = getgroups(ngroups, gidset);
+		}
 		/* The default gid might not be in the list on some systems. */
 		for (n = 0; n < ngroups; n++) {
 			if ((gid_t)gidset[n] == our_gid)
--- a/flist.c	2026-07-24 13:14:37
+++ b/flist.c	2026-08-28 23:23:10
@@ -1208,6 +1208,13 @@
 	if (preserve_gid) {
 		F_GROUP(file) = gid;
 		file->flags |= gid_flags;
+		/* mac-rsync-patch phase 2: under --chown/
+		 * --groupmap only regular files take the mapped group.  Marking
+		 * directories (or other non-regular entries) made macOS files
+		 * created inside inherit the tag via directory group inheritance,
+		 * so keep them at the receiver's default group instead. */
+		if (groupmap && !S_ISREG(file->mode))
+			file->flags |= FLAG_SKIP_GROUP;
 	}
 	if (atimes_ndx && !S_ISDIR(mode))
 		F_ATIME(file) = atime;
--- a/uidlist.c	2026-08-28 23:23:46
+++ b/uidlist.c	2026-08-28 23:23:10
@@ -515,6 +515,11 @@
 	if (preserve_gid && (!am_root || !numeric_ids || groupmap)) {
 		for (i = 0; i < flist->used; i++) {
 			F_GROUP(flist->files[i]) = match_gid(F_GROUP(flist->files[i]), &flist->files[i]->flags);
+			/* mac-rsync-patch phase 2: mirror of
+			 * the recv_file_entry() hunk for the non-inc_recurse path --
+			 * --chown/--groupmap only marks regular files. */
+			if (groupmap && !S_ISREG(flist->files[i]->mode))
+				flist->files[i]->flags |= FLAG_SKIP_GROUP;
 		}
 	}
 }
