'use strict';

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

const SALT = process.env.AKUPARA_SALT || 'akupara-salt';
const TIME = 2;
const MEM = 65536;
const HASH_LEN = 32;
const PARALLELISM = 1;

const password = process.argv[2];
if (!password) {
    console.error('Usage: node generate-password-hash.js <password>');
    console.error('Prints the Argon2id encoded hash to use as the PASSWORD value in .env');
    process.exit(1);
}

argon2.hash({
    pass: password,
    salt: SALT,
    time: TIME,
    mem: MEM,
    hashLen: HASH_LEN,
    parallelism: PARALLELISM,
    type: argon2.ArgonType.Argon2id,
})
    .then((result) => console.log(result.encoded))
    .catch((error) => {
        console.error(error.message || error);
        process.exit(1);
    });