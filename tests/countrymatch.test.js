const test = require('node:test');
const assert = require('node:assert/strict');
const CM = require('../static/countrymatch.js');

test('exact match Indonesia', () => {
  assert.equal(CM.match('Indonesia', ['Russia', 'Indonesia', 'Iran']), 'Indonesia');
});

test('reversed-token match North Korea', () => {
  assert.equal(
    CM.match('North Korea', ['Korea, North', 'Korea, South', 'Japan']),
    'Korea, North'
  );
});

test('containment picks fullest match for DRC', () => {
  const available = ['Congo, Republic of the', 'Congo, Democratic Republic of the'];
  assert.equal(CM.match('Democratic Republic of the Congo', available), 'Congo, Democratic Republic of the');
});

test('alias Myanmar to Burma', () => {
  assert.equal(CM.match('Myanmar', ['Burma', 'Thailand']), 'Burma');
});

test('accents and punctuation Cote d Ivoire', () => {
  assert.equal(CM.match("Côte d'Ivoire", ['Cote d Ivoire', 'Ghana']), 'Cote d Ivoire');
});

test('containment match North Macedonia', () => {
  assert.equal(
    CM.match('North Macedonia', ['North Macedonia, The Republic of', 'Albania']),
    'North Macedonia, The Republic of'
  );
});

test('no match returns null', () => {
  assert.equal(CM.match('Atlantis', ['Russia', 'Iran']), null);
});

test('empty geo returns null', () => {
  assert.equal(CM.match('', ['Russia', 'Iran']), null);
});