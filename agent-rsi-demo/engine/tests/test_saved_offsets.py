"""Physical dx is restored by identity and never silently shifts a column."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sa_common import load_local
from sa_saved import load_placement


class SavedOffsetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder=Path(self.tmp.name)
        self.source=folder/'test.cir'
        self.source.write_text('R1 a 0 1k\nR2 a b 2k\n',encoding='utf8')
        self.path=folder/'test-placement.json'
        self.data={'source_netlist':str(self.source),'placement':{
            'R1':{'row':0,'col':0,'orient':0},'R2':{'row':1,'col':1,'orient':1}},
            'layout_grid':{'column_x':[100,200,300],'row_y':[100,200]}}

    def load(self):
        self.path.write_text(json.dumps(self.data),encoding='utf8')
        return load_placement(self.path)

    def test_legacy_is_zero_offset_and_optional_fields_follow_identity(self):
        old=self.load(); pl=old[3];index=old[6]
        original=pl.pin_world(index['R1'],'1')
        self.assertFalse(hasattr(pl,'offset_x'))
        self.data['placement']['R1']['dx']=20
        loaded=self.load(); shifted=loaded[3];index=loaded[6]
        self.assertEqual((120,100),shifted.pos_of(index['R1']))
        self.assertEqual((200,200),shifted.pos_of(index['R2']))
        self.assertEqual((original[0]+20,original[1]),shifted.pin_world(index['R1'],'1'))

    def test_invalid_offsets_and_crossing_midpoints_are_rejected(self):
        for value in (True,'20',float('nan'),float('inf'),51,-51):
            self.data['placement']['R1']['dx']=value
            with self.subTest(value=value),self.assertRaises(ValueError):self.load()

    def test_spacing_enlarges_only_axes_preserving_local_dx(self):
        import spacing_retry
        self.data['placement']['R1']['dx']=20
        _,_,_,pl,comps,nets,index=self.load()
        before=deepcopy({key:value for key,value in pl.__dict__.items() if key!='rng'})
        rng_before=pl.rng.getstate()
        def route(par,nets,trial,index,**kwargs):
            self.assertEqual(20,trial.offset_x[index['R1']])
            self.assertEqual((120,100),trial.pos_of(index['R1']))
            self.assertEqual((250,250),trial.pos_of(index['R2']))
            return [],{'wirelength':0,'crossings':0,'bends':0},{'valid':True}
        with patch('spacing_retry.final_route',side_effect=route),patch('spacing_retry.evaluate_quality',return_value={'score':0}):
            result=spacing_retry.retry_spacing((pl,comps,nets,index,{}),scale=1.5)
        self.assertEqual(before,{key:value for key,value in pl.__dict__.items() if key!='rng'})
        self.assertEqual(rng_before,pl.rng.getstate())
        self.assertEqual(result[0].offset_x,pl.offset_x)


if __name__=='__main__':unittest.main()
