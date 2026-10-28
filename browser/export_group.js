/* ============================================================================
   OSINT Group Exporter — chạy trong Console (F12) của trang Facebook đã đăng nhập
   Hoặc bấm bookmarklet (xem README).

   Cách dùng:
     1. Mở trang nhóm (đã đăng nhập) — nên mở tab "Mọi người" / "Thành viên"
     2. F12 → Console
     3. Dán toàn bộ nội dung file này → Enter
     4. File JSON sẽ tự tải về (đặt tên group_<id>_<timestamp>.json)
     5. Bỏ file .json vào thư mục html_dump → dashboard bấm "Nạp html_dump"
   ============================================================================ */
(function () {
  var txt = function () { return (document.body.innerText || ""); };
  var T = txt();

  // ---------- helpers ----------
  var uniq = function (a) { return Array.from(new Set(a.filter(Boolean))); };

  function matchInt(s) {
    if (!s) return null;
    var m = String(s).replace(/\./g, "").match(/(\d[\d.,]*)\s*(nghìn|nghin|ngàn|ngan|triệu|trieu|tr|K|k|M|m)?/i);
    if (!m) return null;
    var v = parseFloat(m[1].replace(/,/g, "."));
    if (isNaN(v)) return null;
    var u = (m[2] || "").toLowerCase();
    if (u === "nghìn" || u === "nghin" || u === "ngàn" || u === "ngan" || u === "k") v *= 1e3;
    else if (u === "triệu" || u === "trieu" || u === "tr" || u === "m") v *= 1e6;
    return Math.round(v);
  }

  // ---------- id & tên nhóm ----------
  var url = location.href;
  var gid = (url.match(/groups\/(\d{6,})/) || [])[1] || null;
  var h1 = document.querySelector("h1");
  var name = (h1 ? h1.innerText : "").split("\n")[0].trim() || document.title.split("|")[0].trim();

  // ---------- quyền riêng tư ----------
  var privacy = "unknown";
  if (/Nhóm Công khai|Public Group/i.test(T)) privacy = "public";
  else if (/Nhóm Kín|Closed Group/i.test(T)) privacy = "closed";
  else if (/Nhóm Riêng tư|Private Group/i.test(T)) privacy = "secret";

  // ---------- số thành viên ----------
  var members = null;
  var mm = T.match(/([\d.,]+\s*(?:nghìn|nghin|ngàn|ngan|triệu|trieu|K|M)?)\s*(?:thành viên|Thành viên)/);
  if (mm) members = matchInt(mm[1]);
  if (members === null) {
    var mm2 = T.match(/(?:Thành viên|Members)\s*[·:]\s*([\d.,]+)/);
    if (mm2) members = matchInt(mm2[1]);
  }

  // ---------- gom link profile + tên hiển thị ----------
  // Bấm vào tên -> popup, nên lấy theo cặp (container, link) gần nhau.
  var people = [];
  var seenProfile = {};

  // Vị trí các khu vực trong text (dùng để gán vai trò)
  var iAdmin = T.search(/Quản trị viên và người kiểm duyệt|Quản trị viên và kiểm duyệt|Moderators and Admins|Quản trị viên/i);
  var iMember = T.search(/Thành viên điểm chung|Thành viên\s*[·:]|^Members/im);

  function indexOfZone(label) {
    var at = T.indexOf(label);
    if (at < 0) return "unknown";
    if (iAdmin >= 0 && at >= iAdmin && (iMember < 0 || at < iMember)) return "admin";
    if (iMember >= 0 && at >= iMember) return "member";
    return "unknown";
  }

  var links = document.querySelectorAll('a[href*="/groups/' + gid + '"], a[href*="profile.php?id="]');
  Array.prototype.forEach.call(links, function (a) {
    var href = a.href.split("?")[0];
    var isProfile = /profile\.php\?id=\d+/.test(href);
    var m = href.match(/\/groups\/(\d+)\/(?:user|people)\/([\w.\-]+)/);
    if (m) href = "https://www.facebook.com/" + m[2];
    if (!m && !isProfile) return;
    if (seenProfile[href]) return;
    // tên: text của thẻ chứa link, hoặc chính text link
    var box = a.closest("div");
    var label = (a.innerText || "").trim();
    if (!label && box) label = (box.innerText || "").trim().split("\n")[0];
    if (!label || label.length > 80) return;
    label = label.split("\n")[0].trim();   // chỉ giữ dòng đầu (tên người)
    seenProfile[href] = 1;
    people.push({ url: href, label: label, zone: indexOfZone(label) });
  });

  // ---------- phân vai trò (dùng zone đã gán khi thu thập) ----------
  var admins = [], moderators = [], memberList = [];
  people.forEach(function (p) {
    if (p.zone === "admin") admins.push(p);
    else if (p.zone === "member") memberList.push(p);
    else admins.push(p); // không xác định -> đưa vào nhóm chung để không bỏ sót
  });

  // ---------- bài đăng (nếu đang mở tab bài viết) ----------
  var posts = [];
  var arts = document.querySelectorAll('div[role="article"]');
  Array.prototype.forEach.call(arts, function (a, idx) {
    var t = (a.innerText || "").trim();
    if (t.length < 30) return;
    posts.push({
      index: idx,
      text: t.slice(0, 1200),
      time: (t.match(/\d+\s*(?:phút|giờ|ngày|tuần|tháng)\s*trước|Hôm qua[\s\S]{0,20}?\d{1,2}:\d{2}|\d{1,2}\s*tháng\s*\d{1,2}[\s\S]{0,15}?\d{1,2}:\d{2}/i) || [""])[0]
    });
  });

  // ---------- xuất file ----------
  var out = {
    schema: "osint-group-export/1",
    exported_at: new Date().toISOString(),
    source_url: url,
    group: {
      id: gid,
      name: name,
      privacy: privacy,
      member_count: members,
      admins: admins,
      moderators: mods,
      members: memberList,
      posts: posts
    }
  };

  var blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "group_" + (gid || "unknown") + "_" +
    new Date().toISOString().replace(/[:T]/g, "").slice(0, 14) + ".json";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  console.log("[OSINT] Da tai file:", a.download);
  console.log("[OSINT] Nhom:", name, "| thanh vien:", members,
              "| admins:", admins.length, "| members:", memberList.length,
              "| bai dang:", posts.length);
})();