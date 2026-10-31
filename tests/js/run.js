// Run DOM-free JavaScript modules against a JSON vector file, under gjs.
//
//     gjs -m tests/js/run.js VECTORS.json
//
// Exit status: 0 when every case passes, 1 when any fails, 2 when the vectors
// or a module cannot be loaded. The Python side (tests/harness/js.py) runs
// this and skips when gjs is not installed.
//
// A vector file:
//
//   { "modules": { "Model": "selftest_model.js" },   // paths relative to the vector file
//     "cases": [
//       { "name": "adds",  "call": "Model.add", "args": [1, 2], "expect": 3 },
//       { "name": "store", "call": "Model.makeCounter", "args": [], "save": "c" },
//       { "name": "use",   "call": "$c.inc", "args": [], "expect": 1 },
//       { "name": "ref",   "call": "Model.size", "args": [{ "$ref": "c" }], "expect": 1 } ] }
//
// "modules" names each module's exports, as app.js imports them (`import * as
// KleinRecording from "./recording.js"`). In "args", {"$base64": "..."} stands
// for those bytes as an ArrayBuffer.
//
// "call" names a function (a dotted path: a module's name, then its export)
// or, with a leading "$", a method on a value an earlier case saved; "get" in
// its place names a value the same way and reads it. "expect" is
// compared as canonical JSON: object keys sorted, a Map as its entries sorted
// by key, a Set as its sorted values. Every case needs an "expect", except a
// setup case that only "save"s its result.
//
// The modules are the dashboard's own ES modules, imported as the browser
// does (no build step), each once: one that imports another shares it, as in
// the page.

import System from "system";
import GLib from "gi://GLib";

function readText(path) {
    const [, bytes] = GLib.file_get_contents(path);
    return new TextDecoder().decode(bytes);
}

function canonical(value) {
    if (value === undefined) return "undefined";
    // Numbers in numeric order, anything else by its canonical text.
    const order = (a, b) => {
        if (typeof a === "number" && typeof b === "number") return a - b;
        const [x, y] = [canonical(a), canonical(b)];
        return x < y ? -1 : x > y ? 1 : 0;
    };
    return JSON.stringify(value, (key, v) => {
        // Plain JSON would write both as {}, letting any Map or Set pass.
        if (v instanceof Map) return [...v.entries()].sort((p, q) => order(p[0], q[0]));
        if (v instanceof Set) return [...v].sort(order);
        if (v && typeof v === "object" && !Array.isArray(v)) {
            return Object.keys(v).sort().reduce((o, k) => { o[k] = v[k]; return o; }, {});
        }
        return v;
    });
}

// [the value at a dotted path, the object holding it].
function lookup(api, saved, name) {
    const path = name.split(".");
    let value = api, self;
    if (name.startsWith("$")) {
        value = saved[path.shift().slice(1)];
    }
    for (const part of path) {
        self = value;
        value = value == null ? undefined : value[part];
    }
    return [value, self];
}

function resolve(api, saved, call) {
    const [fn, self] = lookup(api, saved, call);
    if (typeof fn !== "function") throw new Error(`"${call}" is not a function`);
    return [fn, self];
}

function substitute(value, saved) {
    if (Array.isArray(value)) return value.map((v) => substitute(v, saved));
    if (value && typeof value === "object") {
        if ("$ref" in value) return saved[value.$ref];
        if ("$base64" in value) {           // binary data, as an ArrayBuffer
            const bytes = GLib.base64_decode(value.$base64);
            return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        }
        return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, substitute(v, saved)]));
    }
    return value;
}

async function main(argv) {
    const vectorsPath = GLib.canonicalize_filename(argv[0], GLib.get_current_dir());
    let vectors;
    const api = {};
    try {
        vectors = JSON.parse(readText(vectorsPath));
        for (const [name, module] of Object.entries(vectors.modules)) {
            const path = GLib.canonicalize_filename(module, GLib.path_get_dirname(vectorsPath));
            api[name] = await import(GLib.filename_to_uri(path, null));
        }
    } catch (e) {
        print(`load error: ${e}`);
        return 2;
    }

    const saved = {};
    let failed = 0;
    for (const [i, c] of vectors.cases.entries()) {
        let ok = true, why = "", result;
        try {
            if ("get" in c) {
                result = lookup(api, saved, c.get)[0];
            } else {
                const [fn, self] = resolve(api, saved, c.call);
                result = fn.apply(self, substitute(c.args || [], saved));
            }
            if (!("expect" in c) && !c.save) {
                ok = false;
                why = "no \"expect\": the case checks nothing";
            } else if ("expect" in c && canonical(result) !== canonical(c.expect)) {
                ok = false;
                why = `expected ${canonical(c.expect)}\n     got ${canonical(result)}`;
            }
        } catch (e) {
            ok = false;
            why = `threw ${e}`;
        }
        if (c.save) saved[c.save] = result;
        if (!ok) failed++;
        print(`${ok ? "ok" : "not ok"} ${i + 1} - ${c.name}${ok ? "" : "\n     " + why}`);
    }
    if (vectors.cases.length === 0) {
        print("not ok - the vector file holds no cases");
        return 1;
    }
    print(`# ${vectors.cases.length - failed}/${vectors.cases.length} passed`);
    return failed ? 1 : 0;
}

System.exit(await main(System.programArgs));
