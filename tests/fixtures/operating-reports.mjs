/** Immutable public-source regression cohort captured November 20, 2026.
 * These are the19 reviewed disclosures through November13. Live financials.json
 * may legitimately change; tests must not turn this fixture into a release gate
 * against new reports, corrections or backfills. No private article data.
 */
const fields = ['gross_revenue','market_making_costs','net_revenue','rewards','net_profit','treasury_inflow','olp_pnl'];
const labels = {
  gross_revenue:'2w Spreads Paid (Gross Revenue)',
  market_making_costs:'2w Market Making Costs (Cost of Revenue)',
  net_revenue:'2w Net Revenue',rewards:'2w Rewards',net_profit:'2w Net Profit',
};
// id, actual UTC publication, signed period amounts, canonical sources, warnings.
const records = [
  ["x:2029983510824636491","2026-03-06T18:12:38Z",["1758330","-1140903","617427","-260884","356543","175833","180710"],["https://x.com/variational_io/status/2029983510824636491"],[]],
  ["x:2035011447072440602","2026-03-20T15:11:52Z",["1763330","-755767","1007563","-262014","745549","176333","569216"],["https://x.com/variational_io/status/2035011447072440602"],[]],
  ["x:2040164994764124195","2026-04-03T20:30:13Z",["1556120","-717916","838204","-122261","715943","263048","452895"],["https://x.com/variational_io/status/2040164994764124195"],[]],
  ["x:2045195227615613390","2026-04-17T17:38:34Z",["1563380","-966025","597355","-75856","521499","312676","208823"],["https://x.com/variational_io/status/2045195227615613390"],[]],
  ["x:2050242620912537798","2026-05-01T15:55:07Z",["1426600","-1108556","318044","-69545","248499","285320","-36821"],["https://x.com/variational_io/status/2050242620912537798"],[]],
  ["x:2055324287922507828","2026-05-15T16:27:50Z",["1687700","-1011619","676081","-81710","594371","337540","256831"],["https://x.com/variational_io/status/2055324287922507828"],[]],
  ["x:2060738032580567428","2026-05-30T15:00:08Z",["2243040","-1272376","970664","-107553","863111","448608","414503"],["https://x.com/variational_io/status/2060738032580567428"],[]],
  ["x:2065467514486993157","2026-06-12T16:13:24Z",["4332160","-2156274","2175886","-209668","1966218","866432","1099786"],["https://x.com/variational_io/status/2065467514486993157"],[]],
  ["x:2070877921158586432","2026-06-27T14:32:26Z",["3216760","-1947440","1269320","-155995","1113325","643352","469973"],["https://x.com/variational_io/status/2070877921158586432"],[]],
  ["x:2075603602081824975","2026-07-10T15:30:36Z",["3069800","-1652505","1417295","-148583","1268712","613960","654752"],["https://x.com/variational_io/status/2075603602081824975"],[]],
  ["x:2080694730460151877","2026-07-24T16:40:55Z",["2380440","-1365199","1015241","-115932","899309","476088","423221"],["https://x.com/variational_io/status/2080694730460151877"],[]],
  ["x:2085772888293228829","2026-08-07T16:59:43Z",["2768210","-1393457","1374753","-135371","1239387","553642","685745"],["https://x.com/variational_io/status/2085772888293228829"],[{"code":"net_profit_identity","residual":"-5","unit":"$"}]],
  ["x:2090861899730812971","2026-08-21T18:01:37Z",["3158920",null,"1167121","-155016","1322137","631784","690353"],["https://x.com/variational_io/status/2090861899730812971"],[{"code":"net_profit_identity","residual":"-310032","unit":"$"}]],
  ["x:2095924511921336444","2026-09-04T17:18:38Z",["5080820",null,"2278641","-247501","2031140","1016164","1014976"],["https://x.com/variational_io/status/2095924511921336444"],[]],
  ["x:2100975039118225440","2026-09-18T15:47:38Z",["4570970",null,"2479527","-211814","2267713","914194","1353519"],["https://x.com/variational_io/status/2100975039118225440"],[]],
  ["x:2106063274114723997","2026-10-02T16:46:27Z",["7372480",null,"3383789","-337984","3045805","1474496","1571309"],["https://x.com/variational_io/status/2106063274114723997"],[]],
  ["x:2111139394547818854","2026-10-16T16:57:09Z",["6561755",null,"3682137","-303768","3378369","1312351","2066018"],["https://x.com/variational_io/status/2111139394547818854"],[{"code":"lifetime_olp_change","residual":"273","unit":"$"}]],
  ["x:2116210079758524489","2026-10-30T16:36:34Z",["5233390",null,"2341767","-236977","2104790","1046678","1058112"],["https://t.me/variational_io/446","https://x.com/variational_io/status/2116210079758524489"],[]],
  ["x:2121317860740063501","2026-11-13T18:51:03Z",["4908930",null,"3208936","-217813","2991123","981786","2009337"],["https://t.me/variational_io/459","https://x.com/variational_io/status/2121317860740063501"],[]]
];
function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
export const frozenOperatingReports = freeze(records.map(([id,published_at,values,source_urls,warnings]) => {
  const period_amounts = Object.fromEntries(fields.map((key,index) => [key,values[index]]));
  const currentLabels = Date.parse(published_at) >= Date.parse('2026-10-16T00:00:00Z');
  const sourceLabels = {...labels,treasury_inflow:currentLabels?'2W Protocol Treasury Inflow':'Protocol Treasury',olp_pnl:currentLabels?'2W OLP PnL':'OLP PnL'};
  const present = fields.filter(key => period_amounts[key] !== null);
  return {id,published_at,period_label:'2w',period_start:null,period_end:null,
    period_amounts,source_urls,warnings,
    labels:Object.fromEntries(present.map(key => [key,sourceLabels[key]])),
    units:Object.fromEntries(present.map(key => [key,'$'])),
  };
}));
