/*
 * Vanilla canvas adapter for thinking-orbs 0.1.1.
 *
 * Calliope is intentionally framework-free, so this keeps the three states
 * DataRabbit uses (working/orbits, composing/ribbon, solving/rubik) while
 * preserving the library's drawing math and tuned 64px chat preset.
 *
 * MIT License
 * Copyright (c) 2026 Jakub Antalik
 * https://github.com/Jakubantalik/thinking-orbs
 */
(() => {
  "use strict";

  const active = new WeakMap();
  const prefersReducedMotion = () =>
    window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;

  function seeded(index, salt) {
    const value = Math.sin(index * 12.9898 + salt * 78.233) * 43758.5453;
    return value - Math.floor(value);
  }

  function rotate(yaw, pitch, centerX, centerY, scale) {
    const sinPitch = Math.sin(pitch);
    const cosPitch = Math.cos(pitch);
    const sinYaw = Math.sin(yaw);
    const cosYaw = Math.cos(yaw);
    return (x, y, z) => {
      const x1 = x * cosYaw + z * sinYaw;
      const z1 = -x * sinYaw + z * cosYaw;
      const y1 = y * cosPitch - z1 * sinPitch;
      const z2 = y * sinPitch + z1 * cosPitch;
      return [centerX + x1 * scale, centerY - y1 * scale, z2];
    };
  }

  function drawDots(ctx, dots, dark = true, minRadius = 0.3, tint = "") {
    dots.sort((left, right) => left.z - right.z);
    for (const dot of dots) {
      const alpha = dot.a ?? 1;
      if (alpha < 0.02) continue;
      const white = Math.min(1, Math.max(0, dot.white));
      const intensity = dark ? 1 - white : white;
      if (tint) {
        ctx.fillStyle = tint;
        ctx.globalAlpha = alpha * (0.2 + 0.8 * intensity);
      } else {
        const channel = Math.round(intensity * 255);
        ctx.fillStyle = `rgba(${channel},${channel},${channel},${alpha})`;
      }
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, Math.max(minRadius, dot.r), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function radiusScale(size, exponent) {
    return (size / 300) ** exponent;
  }

  function moveProgress(time, count, moveSeconds, pauseSeconds) {
    const cycle = 2 * count * moveSeconds + pauseSeconds;
    const offset = time % cycle;
    const amount = new Array(count).fill(0);
    let activeMove = -1;
    if (offset < 2 * count * moveSeconds) {
      const step = Math.floor(offset / moveSeconds);
      const local = (offset - step * moveSeconds) / moveSeconds;
      const eased = 1 - (1 - Math.min(1, local / 0.7)) ** 3;
      if (step < count) {
        for (let index = 0; index < step; index++) amount[index] = 1;
        amount[step] = eased;
        activeMove = step;
      } else {
        const index = 2 * count - 1 - step;
        for (let before = 0; before < index; before++) amount[before] = 1;
        amount[index] = 1 - eased;
        activeMove = index;
      }
    }
    return { amount, active: activeMove };
  }

  function applyMoves(point, moves, progress) {
    let [x, y, z] = point;
    let active = false;
    for (let index = 0; index < moves.length; index++) {
      if (progress.amount[index] <= 0) continue;
      const move = moves[index];
      const coordinate = move.axis === 0 ? x : move.axis === 1 ? y : z;
      if (coordinate < move.lo || coordinate >= move.hi) continue;
      if (index === progress.active) active = true;
      const angle = move.angle * progress.amount[index];
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      if (move.axis === 0) {
        const nextY = y * cos - z * sin;
        z = y * sin + z * cos;
        y = nextY;
      } else if (move.axis === 1) {
        const nextX = x * cos + z * sin;
        z = -x * sin + z * cos;
        x = nextX;
      } else {
        const nextX = x * cos - y * sin;
        y = x * sin + y * cos;
        x = nextX;
      }
    }
    return [x, y, z, active];
  }

  function rubikMoves(count) {
    const moves = [];
    for (let index = 0; index < count; index++) {
      const axis = Math.min(2, Math.floor(seeded(index, 2.3) * 3));
      const lo = -1 + 0.5 * Math.min(3, Math.floor(seeded(index, 5.9) * 4));
      const direction = seeded(index, 7.7) < 0.5 ? 1 : -1;
      moves.push({ axis, lo, hi: lo + 0.5, angle: direction * Math.PI / 2 });
    }
    return moves;
  }

  function drawSolving(ctx, size, time, tint = "") {
    const center = size / 2;
    const radius = size / 2 * 0.82;
    const project = rotate(
      time * 0.55,
      0.35 + 0.1 * Math.sin(time * 0.9),
      center,
      center,
      radius,
    );
    const dotScale = radiusScale(size, 0.6);
    const moves = rubikMoves(14);
    const progress = moveProgress(time, moves.length, 0.42, 1.2);
    const dots = [];
    const latRings = 9;
    const lonDensity = 24;
    for (let ring = 0; ring <= latRings; ring++) {
      const latitude = -Math.PI / 2 + ring / latRings * Math.PI;
      const latitudeRadius = Math.cos(latitude);
      const y = Math.sin(latitude);
      const count = Math.max(1, Math.round(Math.abs(latitudeRadius) * lonDensity));
      for (let point = 0; point < count; point++) {
        const longitude = point / count * 2 * Math.PI;
        const [x1, y1, z1, activeMove] = applyMoves(
          [latitudeRadius * Math.cos(longitude), y, latitudeRadius * Math.sin(longitude)],
          moves,
          progress,
        );
        const [x, projectedY, z] = project(x1, y1, z1);
        const depth = (z + 1) / 2;
        dots.push({
          x,
          y: projectedY,
          z,
          r: (0.63 + 1.79 * depth + (activeMove ? 0.32 : 0)) * dotScale,
          white: 0.62 - 0.54 * depth - (activeMove ? 0.14 : 0),
        });
      }
    }
    drawDots(ctx, dots, true, 0.3, tint);
  }

  function drawWorking(ctx, size, time, tint = "") {
    const center = size / 2;
    const radius = size / 2 * 0.82;
    const project = rotate(time * 0.12, 0.3, center, center, 1);
    const dotScale = radiusScale(size, 0.6);
    const dots = [];
    const orbitCount = 12;
    const ghostCount = 40;
    const particles = 3;
    for (let orbit = 0; orbit < orbitCount; orbit++) {
      const randomRadius = seeded(orbit, 1.7);
      const inclination = seeded(orbit, 5.2);
      const randomSpeed = seeded(orbit, 8.9);
      const orbitRadius = radius * (0.45 + 0.52 * randomRadius);
      const longitude = randomRadius * 2 * Math.PI;
      const polar = Math.acos(2 * inclination - 1);
      const normalX = Math.sin(polar) * Math.cos(longitude);
      const normalY = Math.cos(polar);
      const normalZ = Math.sin(polar) * Math.sin(longitude);
      let basisX = -normalY;
      let basisY = normalX;
      const basisZ = 0;
      const length = Math.max(1e-6, Math.sqrt(basisX * basisX + basisY * basisY));
      basisX /= length;
      basisY /= length;
      const crossX = normalY * basisZ - normalZ * basisY;
      const crossY = normalZ * basisX - normalX * basisZ;
      const crossZ = normalX * basisY - normalY * basisX;
      const speed = (0.25 + 0.55 * randomSpeed) * (randomSpeed > 0.5 ? 1 : -1);
      for (let point = 0; point < ghostCount; point++) {
        const angle = point / ghostCount * 2 * Math.PI;
        const [x, y, z] = project(
          (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
          (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
          (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
        );
        const depth = (z / orbitRadius + 1) / 2;
        dots.push({
          x,
          y,
          z,
          r: 0.9 * dotScale,
          white: 0.72,
          a: 0.5 * (0.4 + 0.6 * depth),
        });
      }
      for (let particle = 0; particle < particles; particle++) {
        const angle = time * speed + particle / particles * 2 * Math.PI + inclination * 6;
        const [x, y, z] = project(
          (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
          (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
          (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
        );
        const depth = (z / orbitRadius + 1) / 2;
        dots.push({
          x,
          y,
          z,
          r: (1.2 + 1.6 * depth) * dotScale,
          white: 0.3 - 0.22 * depth,
        });
      }
    }
    drawDots(ctx, dots, true, 0.3, tint);
  }

  function fibonacciSphere(index, count) {
    const golden = Math.PI * (3 - Math.sqrt(5));
    const y = 1 - 2 * (index + 0.5) / count;
    const radius = Math.sqrt(1 - y * y);
    const angle = index * golden;
    return [radius * Math.cos(angle), y, radius * Math.sin(angle)];
  }

  function drawComposing(ctx, size, time, tint = "") {
    const center = size / 2;
    const radius = size / 2 * 0.78;
    const project = rotate(0, 0.3, center, center, 1);
    const dotScale = radiusScale(size, 0.6);
    const dots = [];
    for (let point = 0; point < 38; point++) {
      const sphere = fibonacciSphere(point, 38);
      const [x, y, z] = project(sphere[0] * radius, sphere[1] * radius, sphere[2] * radius);
      const depth = (z / radius + 1) / 2;
      dots.push({ x, y, z, r: 0.8 * dotScale, white: 0.78, a: 0.1 + 0.22 * depth });
    }
    const yaw = 0;
    const pitch = 0.55;
    const yawCos = Math.cos(yaw);
    const yawSin = Math.sin(yaw);
    const basisA = [yawCos, 0, yawSin];
    const basisB = [-yawSin * Math.sin(pitch), Math.cos(pitch), yawCos * Math.sin(pitch)];
    const basisC = [
      basisA[1] * basisB[2] - basisA[2] * basisB[1],
      basisA[2] * basisB[0] - basisA[0] * basisB[2],
      basisA[0] * basisB[1] - basisA[1] * basisB[0],
    ];
    const lanes = 3;
    const segments = 44;
    for (let lane = 0; lane < lanes; lane++) {
      const offset = (lane - (lanes - 1) / 2) * 0.075;
      const distance = Math.abs(lane - (lanes - 1) / 2) / Math.max(1, (lanes - 1) / 2);
      for (let segment = 0; segment < segments; segment++) {
        const angle = segment / segments * 2 * Math.PI;
        const wobble =
          0.16 * Math.sin(angle * 3 - time * 1.7 + lane * 0.22)
          + 0.07 * Math.sin(angle * 5 + time * 1.1);
        const depthOffset = offset + wobble;
        const x1 = basisA[0] * Math.cos(angle) + basisB[0] * Math.sin(angle) + basisC[0] * depthOffset;
        const y1 = basisA[1] * Math.cos(angle) + basisB[1] * Math.sin(angle) + basisC[1] * depthOffset;
        const z1 = basisA[2] * Math.cos(angle) + basisB[2] * Math.sin(angle) + basisC[2] * depthOffset;
        const length = Math.sqrt(x1 * x1 + y1 * y1 + z1 * z1);
        const [x, y, z] = project(x1 / length * radius, y1 / length * radius, z1 / length * radius);
        const depth = (z / radius + 1) / 2;
        dots.push({
          x,
          y,
          z,
          r: (0.935 + 1.445 * depth) * (1 - 0.25 * distance) * dotScale,
          white: 0.52 - 0.44 * depth + 0.18 * distance,
          a: 0.4 + 0.6 * depth,
        });
      }
    }
    drawDots(ctx, dots, true, 0.3, tint);
  }

  const states = {
    working: { speed: 1.885, draw: drawWorking },
    composing: { speed: 2.34, draw: drawComposing },
    solving: { speed: 1.82, draw: drawSolving },
  };

  function mount(canvas, stateName = "working", size = 64) {
    if (!(canvas instanceof HTMLCanvasElement)) return;
    active.get(canvas)?.();
    const state = states[stateName] || states.working;
    const tint = canvas.dataset.thinkingOrbTint === "theme"
      ? window.getComputedStyle(canvas).color
      : "";
    const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(size * pixelRatio);
    canvas.height = Math.round(size * pixelRatio);
    canvas.style.width = `${size}px`;
    canvas.style.height = `${size}px`;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${stateName[0].toUpperCase()}${stateName.slice(1)}…`);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    let frame = 0;
    let stopped = false;
    const paint = (seconds) => {
      ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      ctx.clearRect(0, 0, size, size);
      state.draw(ctx, size, seconds * state.speed, tint);
    };
    const tick = (now) => {
      if (stopped || !canvas.isConnected) return;
      paint(now / 1000);
      frame = requestAnimationFrame(tick);
    };
    if (prefersReducedMotion()) {
      paint(0.6);
    } else {
      frame = requestAnimationFrame(tick);
    }
    const stop = () => {
      stopped = true;
      cancelAnimationFrame(frame);
    };
    active.set(canvas, stop);
  }

  function unmount(canvas) {
    active.get(canvas)?.();
    active.delete(canvas);
  }

  function mountAll(root = document) {
    root.querySelectorAll("canvas[data-thinking-orb]").forEach((canvas) => {
      if (!active.has(canvas)) {
        const requestedSize = Number(canvas.dataset.thinkingOrbSize);
        const size = Number.isFinite(requestedSize) && requestedSize > 0 ? requestedSize : 64;
        mount(canvas, canvas.dataset.thinkingOrb, size);
      }
    });
  }

  window.CalliopeThinkingOrbs = { mount, unmount, mountAll, states: Object.keys(states) };
})();
