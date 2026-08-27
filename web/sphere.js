/* ------------------------------------------------------------------
   The orb.

   A lat/long wireframe sphere, displaced by smooth 3D value noise so it
   breathes rather than spins like a globe, projected and drawn to a 2D
   canvas. No WebGL and no libraries: this is a few hundred points and a
   couple of thousand line segments, which a 2D context handles at 60fps
   without asking the GPU for anything.

   It carries one piece of information, deliberately. Its state tracks what
   the system is doing:

     idle      slow drift, low amplitude
     thinking  faster rotation, higher amplitude, brighter
     alert     red shift, used when a risk gate has blocked something

   That makes it a status light you can read from across the room, rather
   than decoration that happens to move.
   ------------------------------------------------------------------ */

(function () {
  'use strict';

  const TAU = Math.PI * 2;

  // -- smooth 3D value noise -------------------------------------------
  // Hash-based, so it is deterministic and needs no permutation table.
  function hash(x, y, z) {
    const n = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453;
    return n - Math.floor(n);
  }
  const fade = (t) => t * t * (3 - 2 * t);
  const lerp = (a, b, t) => a + (b - a) * t;

  function noise3(x, y, z) {
    const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
    const xf = fade(x - xi), yf = fade(y - yi), zf = fade(z - zi);
    let out = 0;
    for (let i = 0; i < 2; i++) {
      for (let j = 0; j < 2; j++) {
        const z0 = hash(xi + i, yi + j, zi);
        const z1 = hash(xi + i, yi + j, zi + 1);
        const v = lerp(z0, z1, zf);
        const w = (i ? xf : 1 - xf) * (j ? yf : 1 - yf);
        out += v * w;
      }
    }
    return out * 2 - 1;
  }

  // Tuned to the console's gold on ink palette. hueA is the far side, hueB the
  // near side, so the sphere reads as a volume rather than a flat net.
  const STATES = {
    idle:     { amp: 0.13, speed: 0.10, churn: 0.18, hueA: 30, hueB: 46, light: 52 },
    thinking: { amp: 0.26, speed: 0.34, churn: 0.60, hueA: 26, hueB: 50, light: 62 },
    alert:    { amp: 0.20, speed: 0.22, churn: 0.45, hueA: 8,  hueB: 24, light: 56 }
  };

  class Orb {
    constructor(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.lat = opts.lat || 26;
      this.lon = opts.lon || 34;
      this.state = 'idle';
      this.mix = Object.assign({}, STATES.idle);
      this.t = 0;
      this.rot = 0;
      this.raf = null;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.build();
      this.resize();
      window.addEventListener('resize', () => this.resize());

      // The canvas is often laid out after this runs: the rail starts hidden,
      // so getBoundingClientRect returns 0x0 and the backing store ends up one
      // pixel, which the browser then scales up into a solid coloured square.
      // Watching the element means the first real layout fixes it.
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => this.resize());
        this._ro.observe(canvas);
      }
    }

    // Points are generated once. Only the displacement changes per frame,
    // so there is no per-frame allocation and nothing for the GC to collect.
    build() {
      this.grid = [];
      for (let i = 0; i <= this.lat; i++) {
        const theta = (i / this.lat) * Math.PI;
        const row = [];
        for (let j = 0; j <= this.lon; j++) {
          const phi = (j / this.lon) * TAU;
          row.push({
            x: Math.sin(theta) * Math.cos(phi),
            y: Math.cos(theta),
            z: Math.sin(theta) * Math.sin(phi),
            px: 0, py: 0, pz: 0, scale: 1
          });
        }
        this.grid.push(row);
      }
    }

    resize() {
      const rect = this.canvas.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return;   // not laid out yet
      const w = rect.width, h = rect.height;
      this.canvas.width = Math.round(w * this.dpr);
      this.canvas.height = Math.round(h * this.dpr);
      this.w = w; this.h = h;
      this.radius = Math.min(w, h) * 0.36;
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    }

    setState(name) {
      if (STATES[name]) this.state = name;
    }

    project() {
      const target = STATES[this.state] || STATES.idle;
      // Ease toward the target so a state change reads as the thing
      // accelerating, not as a jump cut.
      for (const k in target) this.mix[k] += (target[k] - this.mix[k]) * 0.045;

      const cos = Math.cos(this.rot), sin = Math.sin(this.rot);
      const tilt = 0.42, ct = Math.cos(tilt), st = Math.sin(tilt);
      const n = this.t * this.mix.churn;

      for (const row of this.grid) {
        for (const p of row) {
          const d = 1 + this.mix.amp *
            noise3(p.x * 1.6 + n, p.y * 1.6 - n * 0.7, p.z * 1.6 + n * 0.4);
          let x = p.x * d, y = p.y * d, z = p.z * d;

          // yaw then pitch
          let rx = x * cos - z * sin;
          let rz = x * sin + z * cos;
          let ry = y * ct - rz * st;
          rz = y * st + rz * ct;

          // Weak perspective. Enough to separate front from back without
          // the fisheye a short focal length would give at this size.
          const persp = 2.6 / (2.6 + rz);
          p.px = rx * this.radius * persp;
          p.py = ry * this.radius * persp;
          p.pz = rz;
          p.scale = persp;
        }
      }
    }

    strokeFor(z, alphaScale) {
      // z runs about -1.2 (back) to 1.2 (front). Map it to hue and alpha so
      // the far side recedes instead of tangling with the near side.
      const t = Math.max(0, Math.min(1, (z + 1.2) / 2.4));
      const hue = lerp(this.mix.hueA, this.mix.hueB, t);
      const alpha = (0.06 + t * 0.58) * (alphaScale === undefined ? 1 : alphaScale);
      return `hsla(${hue.toFixed(0)}, 62%, ${this.mix.light}%, ${alpha.toFixed(3)})`;
    }

    draw() {
      if (!this.w || !this.h) return;
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.save();
      ctx.translate(this.w / 2, this.h / 2);

      // A soft core so the wireframe reads as a volume, not a net.
      const glow = ctx.createRadialGradient(0, 0, this.radius * 0.1, 0, 0, this.radius * 1.5);
      glow.addColorStop(0, `hsla(${this.mix.hueA.toFixed(0)}, 90%, 60%, 0.20)`);
      glow.addColorStop(0.55, `hsla(${this.mix.hueB.toFixed(0)}, 90%, 55%, 0.06)`);
      glow.addColorStop(1, 'hsla(40, 90%, 50%, 0)');
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(0, 0, this.radius * 1.5, 0, TAU);
      ctx.fill();

      ctx.lineWidth = 1;

      // Parallels. Drawn back to front so the near side overlays the far.
      for (let i = 1; i < this.lat; i++) {
        const row = this.grid[i];
        for (let j = 0; j < this.lon; j++) {
          const a = row[j], b = row[j + 1];
          ctx.strokeStyle = this.strokeFor((a.pz + b.pz) / 2);
          ctx.beginPath();
          ctx.moveTo(a.px, a.py);
          ctx.lineTo(b.px, b.py);
          ctx.stroke();
        }
      }

      // Meridians, thinned out so the poles do not turn into a solid disc.
      const step = 2;
      for (let j = 0; j <= this.lon; j += step) {
        for (let i = 0; i < this.lat; i++) {
          const a = this.grid[i][j], b = this.grid[i + 1][j];
          ctx.strokeStyle = this.strokeFor((a.pz + b.pz) / 2, 0.75);
          ctx.beginPath();
          ctx.moveTo(a.px, a.py);
          ctx.lineTo(b.px, b.py);
          ctx.stroke();
        }
      }

      // Vertices on the leading face only, as highlights.
      for (let i = 1; i < this.lat; i += 2) {
        for (let j = 0; j < this.lon; j += 2) {
          const p = this.grid[i][j];
          if (p.pz < 0.55) continue;
          ctx.fillStyle = this.strokeFor(p.pz, 1.15);
          ctx.beginPath();
          ctx.arc(p.px, p.py, 1.25 * p.scale, 0, TAU);
          ctx.fill();
        }
      }

      ctx.restore();
    }

    frame(dt) {
      this.t += dt;
      this.rot += dt * this.mix.speed;
      this.project();
      this.draw();
    }

    start() {
      if (this.raf) return;
      let last = performance.now();
      const loop = (now) => {
        const dt = Math.min((now - last) / 1000, 0.05);
        last = now;
        this.frame(dt);
        this.raf = requestAnimationFrame(loop);
      };
      this.raf = requestAnimationFrame(loop);
    }

    stop() {
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = null;
      if (this._ro) { this._ro.disconnect(); this._ro = null; }
    }
  }

  window.Orb = Orb;
})();
