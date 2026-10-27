'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const argon2Path = path.join(__dirname, '..', 'ui', 'js', 'argon2', 'argon2-bundled.min.js');
const code = fs.readFileSync(argon2Path, 'utf8');
const marker = '721:function(A,I){A.exports="';
const start = code.indexOf(marker);
if (start < 0) {
    console.error('Embedded wasm not found in argon2-bundled.min.js');
    process.exit(1);
}
const base64 = code.slice(start + marker.length, code.indexOf('"', start + marker.length));

global.self = global;
global.Module = {
    wasmBinary: new Uint8Array(Buffer.from(base64, 'base64')),
    locateFile: (file) => file,
};

const argon2 = require(argon2Path);

const TIME = 2;
const MEM = 65536;
const HASH_LEN = 32;
const PARALLELISM = 1;
const CLIENT_SALT_BYTES = 16;
const WRAP_SALT_BYTES = 16;
const PEPPER_BYTES = 32;
const WRAP_PREFIX = 'akupara$2$';
const PEPPER_PATH = path.join(__dirname, '..', 'resources', 'password-pepper');

function base64url(buffer) {
    return buffer.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function createPepper() {
    fs.mkdirSync(path.dirname(PEPPER_PATH), { recursive: true });
    const secret = crypto.randomBytes(PEPPER_BYTES);
    try {
        fs.writeFileSync(PEPPER_PATH, secret.toString('base64') + '\n', { flag: 'wx', mode: 0o600 });
    } catch (error) {
        if (error.code === 'EEXIST') {
            // Another process created the pepper first: use that one.
            return loadPepper();
        }
        throw error;
    }
    console.error('Created the password pepper at ' + PEPPER_PATH + '.');
    console.error('Back it up and keep it out of version control: without it no stored password can be verified.');
    return secret;
}

function loadPepper() {
    let raw;
    try {
        raw = fs.readFileSync(PEPPER_PATH, 'utf8').trim();
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        return createPepper();
    }
    if (!raw) throw new Error('The password pepper at ' + PEPPER_PATH + ' is empty.');
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(raw) || raw.length % 4 !== 0) {
        throw new Error('The password pepper at ' + PEPPER_PATH + ' is not valid base64.');
    }
    const pepper = Buffer.from(raw, 'base64');
    if (pepper.length < PEPPER_BYTES) {
        throw new Error('The password pepper at ' + PEPPER_PATH + ' is too short.');
    }
    return pepper;
}

function wrapVerifier(clientHash) {
    const wrapSalt = crypto.randomBytes(WRAP_SALT_BYTES);
    const message = Buffer.concat([
        Buffer.from('akupara/users/v2\0', 'utf8'),
        wrapSalt,
        Buffer.from('\0', 'utf8'),
        Buffer.from(clientHash, 'utf8'),
    ]);
    const tag = crypto.createHmac('sha256', loadPepper()).update(message).digest();
    return WRAP_PREFIX + base64url(wrapSalt) + '$' + base64url(tag);
}

const username = process.argv[2];
const password = process.argv[3];
if (!username || !password) {
    console.error('Usage: node generate-password-hash.js <username> <password>');
    console.error('Prints a USERS entry for .env; set admin and root before pasting it.');
    console.error('Every USERS entry must be generated this way: raw Argon2id hashes are no longer accepted.');
    process.exit(1);
}

const clientSalt = base64url(crypto.randomBytes(CLIENT_SALT_BYTES));

argon2.hash({
    pass: password,
    salt: clientSalt,
    time: TIME,
    mem: MEM,
    hashLen: HASH_LEN,
    parallelism: PARALLELISM,
    type: argon2.ArgonType.Argon2id,
})
    .then((result) => {
        const entry = {
            username,
            password: wrapVerifier(result.encoded),
            client_salt: clientSalt,
            admin: false,
            root: false,
            id: crypto.randomBytes(16).toString('hex'),
        };
        process.stdout.write(JSON.stringify(entry, null, 2) + '\n');
    })
    .catch((error) => {
        console.error(error.message || error);
        process.exit(1);
    });
