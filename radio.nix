{ config, pkgs, lib, nixpkgs-unstable, ... }:
let
  sourcePw  = "CHANGE-ME";
  radioPort = 8100;

  skipPort       = 8102; # Liquidsoap HTTP (skip, now-playing, metadata), localhost only
  controlPort    = 8103; # radio-control.py (artist search / selection), localhost only
  pagePort       = 8101; # nginx, PUBLIC player page (funnel) + /skip, /np, /meta, /api
  privatePort    = 8104; # nginx, PRIVATE copy of the page + YouTube panel (tailnet only)
  privateApiPort = 8105; # radio-private.py (downloads, library, share), localhost only

  # This machine's Tailscale address. The private page is also served on it, so
  # http://100.x.y.z:8104/ works from any device on the tailnet (nothing public).
  tailscaleIp = "100.x.y.z";

  controlPython = pkgs.python3.withPackages (ps: [ ps.mutagen ]);

  # yt-dlp breaks whenever YouTube changes things, so take it from unstable
  unstable = nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};

  # Everything in /etc/nixos/modules/radio-web/ is served (index.html, icons, manifest).
  # New files must be `git add`ed, since flakes ignore untracked files.
  webroot = pkgs.runCommand "radio-web" { } ''
    mkdir -p $out
    cp -r ${./radio-web}/. $out/
  '';

  ts = "${config.services.tailscale.package}/bin/tailscale";
  icecast = "http://127.0.0.1:${toString radioPort}";

  funnelUp = pkgs.writeShellScript "radio-funnel-up" ''
    set -e  # any failed command fails the service so systemd retries it
    ${ts} funnel --bg --set-path=/radio http://127.0.0.1:${toString pagePort}/
    ${ts} funnel --bg --set-path=/radio.mp3 ${icecast}/radio.mp3
    ${ts} funnel --bg --set-path=/status-json.xsl ${icecast}/status-json.xsl

    # PRIVATE copy (tailnet only, NOT funnel): https://<host>.ts.net:8443/radio/
    ${ts} serve --bg --https=8443 --set-path=/ http://127.0.0.1:${toString privatePort}
    ${ts} serve --bg --https=8443 --set-path=/radio.mp3 ${icecast}/radio.mp3
    ${ts} serve --bg --https=8443 --set-path=/status-json.xsl ${icecast}/status-json.xsl
  '';

  funnelDown = pkgs.writeShellScript "radio-funnel-down" ''
    ${ts} funnel --set-path=/radio off || true
    ${ts} funnel --set-path=/radio.mp3 off || true
    ${ts} funnel --set-path=/status-json.xsl off || true
    ${ts} serve --https=8443 off || true
  '';

  radioScript = pkgs.writeText "radio.liq" ''
    settings.log.level := 3
    settings.harbor.bind_addrs := ["127.0.0.1"]

    # whole library
    all = playlist(mode="randomize", reload_mode="watch", "/mnt/music/FLAC1")

    # artist selection written by radio-control.py (empty file = no selection)
    sel = playlist(mode="randomize", reload_mode="watch", "/var/lib/radio-control/selected.m3u")

    # track shared from the private page (written by radio-private.py; empty file = nothing)
    shared = playlist(mode="normal", loop=false, reload_mode="watch", "/var/lib/radio-private/shared.m3u")

    # shared track first, then the artist selection, then the whole library
    mix   = fallback(track_sensitive=true, [shared, sel, all])
    radio = mksafe(mix)

    # remember the tags of the track being encoded (UTF-8, straight from the files)
    meta_cur = ref([])
    radio.on_metadata(synchronous=true, fun (m) -> meta_cur := m)

    # Skip endpoint (localhost only; nginx rate-limits in front)
    harbor.http.register(
      port=${toString skipPort},
      method="POST",
      "/skip",
      fun (_, response) -> begin
        sel.skip()
        all.skip()
        shared.skip()
        response.html("skipped")
      end
    )

    # Skip only the selection / library track (used by "radio: now" so the freshly
    # shared track itself is not skipped). Localhost only, not exposed by nginx.
    harbor.http.register(
      port=${toString skipPort},
      method="POST",
      "/skipother",
      fun (_, response) -> begin
        sel.skip()
        all.skip()
        response.html("skipped")
      end
    )

    # Now-playing progress for the player page.
    # Returns "<elapsed> <remaining>" in seconds for the track being encoded right now.
    # Read-only: it only reports, it cannot seek or change anything.
    harbor.http.register(
      port=${toString skipPort},
      method="GET",
      "/np",
      fun (_, response) -> begin
        e = source.elapsed(radio)
        r = source.remaining(radio)
        response.content_type("text/plain")
        response.data("#{e} #{r}")
      end
    )

    # Current track tags (artist, title, album) as UTF-8 JSON.
    harbor.http.register(
      port=${toString skipPort},
      method="GET",
      "/meta",
      fun (_, response) -> begin
        m = meta_cur()
        response.content_type("application/json; charset=utf-8")
        response.data(json.stringify(compact=true, {
          artist = m["artist"],
          title  = m["title"],
          album  = m["album"]
        }))
      end
    )

    output.icecast(
      %mp3(bitrate=192, samplerate=44100, stereo=true),
      host="127.0.0.1", port=${toString radioPort},
      password="${sourcePw}",
      mount="radio.mp3",
      name="Voidwerks Radio",
      radio
    )
  '';

  # nginx routes shared by the public page and the private copy of it
  commonLocations = {
    "/".extraConfig = ''
      try_files $uri $uri/ /index.html;
    '';

    # in case the funnel forwards the /radio prefix instead of stripping it
    "/radio/".extraConfig = ''
      rewrite ^/radio/(.*)$ /$1 last;
    '';

    # POST /radio/skip -> Liquidsoap (open to anyone)
    "~ ^(/radio)?/skip$" = {
      proxyPass = "http://127.0.0.1:${toString skipPort}";
      extraConfig = ''
        limit_except POST { deny all; }
        limit_req zone=radioskip nodelay;
        limit_req_status 429;
        rewrite ^ /skip break;
      '';
    };

    # GET /radio/np -> Liquidsoap "elapsed remaining" (progress bar)
    "~ ^(/radio)?/np$" = {
      proxyPass = "http://127.0.0.1:${toString skipPort}";
      extraConfig = ''
        limit_except GET { deny all; }
        rewrite ^ /np break;
        proxy_read_timeout 5s;
        add_header Cache-Control "no-store";
      '';
    };

    # GET /radio/meta -> Liquidsoap track tags (artist, title, album) as UTF-8 JSON
    "~ ^(/radio)?/meta$" = {
      proxyPass = "http://127.0.0.1:${toString skipPort}";
      extraConfig = ''
        limit_except GET { deny all; }
        rewrite ^ /meta break;
        proxy_read_timeout 5s;
        add_header Cache-Control "no-store";
      '';
    };

    # /radio/api/* -> radio-control.py (works whether or not funnel strips /radio)
    "~ ^(/radio)?/api/" = {
      proxyPass = "http://127.0.0.1:${toString controlPort}";
      extraConfig = ''
        rewrite ^(/radio)?/api/(.*)$ /$2 break;
        proxy_read_timeout 20s;
      '';
    };
  };
in
{
  services.icecast = {
    enable = true;
    hostname = "your-host.your-tailnet.ts.net";
    listen.address = "127.0.0.1";
    listen.port = radioPort;
    admin.password = "CHANGE-ME";
    extraConfig = ''
      <location>Homelab</location>
      <admin>you@example.com</admin>
      <authentication>
        <source-password>${sourcePw}</source-password>
      </authentication>
    '';
  };

  # Icecast crashed at boot once ("Can not listen on any sockets"); retry instead of staying dead
  systemd.services.icecast.serviceConfig = {
    Restart = "on-failure";
    RestartSec = 3;
  };

  services.nginx = {
    enable = true;
    # One shared cooldown for skips: at most 1 every 5 seconds, for everyone
    appendHttpConfig = ''
      limit_req_zone $server_name zone=radioskip:64k rate=12r/m;
    '';

    # PUBLIC: local-only static server for the player page (funnel proxies /radio to it)
    virtualHosts."radio-local" = {
      listen = [{ addr = "127.0.0.1"; port = pagePort; }];
      root = webroot;
      locations = commonLocations;
    };

    # PRIVATE: the same page plus the YouTube panel. Only Tailscale Serve (tailnet) points
    # here; the funnel never does, so none of these routes exist on the public address.
    virtualHosts."radio-private" = {
      listen = [
        { addr = "127.0.0.1"; port = privatePort; }   # Tailscale Serve (https://...:8443)
        { addr = tailscaleIp; port = privatePort; }   # http://100.x.y.z:8104/ (tailnet only)
      ];
      root = webroot;
      locations = commonLocations // {
        # same page, with the private panel script injected before </head>
        "/".extraConfig = ''
          try_files $uri $uri/ /index.html;
          sub_filter '</head>' '<script src="/radio/private.js" defer></script></head>';
          sub_filter_once on;
        '';

        "= /radio/private.js" = {
          alias = "${./radio-private.js}";
          extraConfig = ''
            add_header Cache-Control "no-store";
          '';
        };

        # live stream + status, so the player also works on http://<tailscale-ip>:8104/
        "= /radio.mp3" = {
          proxyPass = icecast;
          extraConfig = ''
            proxy_buffering off;
            proxy_read_timeout 3600s;
          '';
        };
        "= /status-json.xsl" = {
          proxyPass = icecast;
          extraConfig = ''
            add_header Cache-Control "no-store";
          '';
        };

        # downloaded audio, played straight from disk (supports seeking)
        "/radio/private-files/" = {
          alias = "/var/lib/radio-private/library/";
          extraConfig = ''
            autoindex off;
            add_header Cache-Control "no-store";
          '';
        };

        # downloads / library / share API (radio-private.py)
        "/radio/private-api/" = {
          proxyPass = "http://127.0.0.1:${toString privateApiPort}";
          extraConfig = ''
            rewrite ^/radio/private-api/(.*)$ /$1 break;
            client_max_body_size 16k;
            proxy_read_timeout 30s;
          '';
        };
      };
    };
  };

  # Dedicated (non-dynamic) user: a DynamicUser's state sits in /var/lib/private,
  # which Liquidsoap's own dynamic user could not read.
  users.users.radio-control = {
    isSystemUser = true;
    group = "radio-control";
  };
  users.groups.radio-control = { };

  users.users.radio-private = {
    isSystemUser = true;
    group = "radio-private";
  };
  users.groups.radio-private = { };

  # selected.m3u / shared.m3u must exist before Liquidsoap starts (empty = nothing)
  systemd.tmpfiles.rules = [
    "d /var/lib/radio-control 0755 radio-control radio-control -"
    "f /var/lib/radio-control/selected.m3u 0644 radio-control radio-control -"
    "d /var/lib/radio-private 0755 radio-private radio-private -"
    "d /var/lib/radio-private/library 0755 radio-private radio-private -"
    "f /var/lib/radio-private/shared.m3u 0644 radio-private radio-private -"
  ];

  systemd.services.radio-control = {
    description = "Radio control (artist search + station selection)";
    wantedBy = [ "multi-user.target" ];
    after = [ "liquidsoap-radio.service" ];
    environment = {
      MUSIC_DIR = "/mnt/music/FLAC1";
      STATE_DIR = "/var/lib/radio-control";
      PORT      = toString controlPort;
      SKIP_URL  = "http://127.0.0.1:${toString skipPort}/skip";
    };
    serviceConfig = {
      ExecStart = "${controlPython}/bin/python3 ${./radio-control.py}";
      User = "radio-control";
      Group = "radio-control";
      StateDirectory = "radio-control";
      StateDirectoryMode = "0755";   # Liquidsoap must be able to read selected.m3u
      ReadOnlyPaths = "/mnt/music/FLAC1";
      Restart = "always";
      RestartSec = 5;
    };
  };

  systemd.services.radio-private = {
    description = "Radio private side (YouTube audio library + share to station)";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [ "network-online.target" "liquidsoap-radio.service" ];
    # yt-dlp needs ffmpeg, plus a JavaScript runtime for YouTube (node, enabled below)
    path = [ unstable.yt-dlp pkgs.ffmpeg pkgs.nodejs ];
    environment = {
      HOME      = "/var/lib/radio-private";
      STATE_DIR = "/var/lib/radio-private";
      LIB_DIR   = "/var/lib/radio-private/library";
      PORT      = toString privateApiPort;
      # "radio: now" skips only the selection/library track, not the shared one
      SKIP_URL  = "http://127.0.0.1:${toString skipPort}/skipother";
      # If YouTube downloads fail with JS/"n challenge" errors, add: --remote-components ejs:github
      YTDLP_EXTRA = "--js-runtimes node";
    };
    serviceConfig = {
      ExecStart = "${controlPython}/bin/python3 ${./radio-private.py}";
      User = "radio-private";
      Group = "radio-private";
      StateDirectory = "radio-private";
      StateDirectoryMode = "0755";   # nginx and Liquidsoap must be able to read the library
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectHome = true;
      Restart = "always";
      RestartSec = 5;
    };
  };

  systemd.services.liquidsoap-radio = {
    description = "Liquidsoap -> Icecast radio";
    wantedBy = [ "multi-user.target" ];
    after = [ "icecast.service" ];
    requires = [ "icecast.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.liquidsoap}/bin/liquidsoap ${radioScript}";
      DynamicUser = true;
      ReadOnlyPaths = "/mnt/music/FLAC1";
      Restart = "always";
      RestartSec = 5;
    };
  };

  systemd.services.tailscale-funnel-radio = {
    description = "Tailscale Funnel (public) + Serve (private) for the radio";
    after = [ "tailscaled.service" "icecast.service" ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    # restart the funnel when the page changes
    restartTriggers = [ webroot ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = funnelUp;
      ExecStop = funnelDown;
      # at boot tailscale may not be connected yet; keep retrying until it is
      Restart = "on-failure";
      RestartSec = 10;
    };
  };

  # nginx can start before tailscale has assigned its address; bind anyway
  boot.kernel.sysctl."net.ipv4.ip_nonlocal_bind" = 1;

  # the private page is reachable on the tailnet interface only
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ privatePort ];

}
