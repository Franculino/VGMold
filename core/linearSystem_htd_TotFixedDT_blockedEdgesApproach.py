"""This module implements red blood cell transport in vascular networks 
discretely, i.e. resolving all RBCs. As would be expected, the computational 
expense increases with network size and timesteps can become very small. An
srXTM sample of ~20000 nodes will take about 1/2h to evolve 1ms at Ht0==0.5.
A performance analysis revealed the worst bottlenecks, which are:
_plot_rbc()
_update_blocked_edges_and_timestep()
Smaller CPU-time eaters are:
_update_flow_and_velocity()
_update_flow_sign()
_propagate_rbc()
"""
from __future__ import division

import numpy as np
from sys import stdout

from copy import deepcopy
from pyamg import smoothed_aggregation_solver, rootnode_solver, util
import pyamg
from scipy import finfo, ones, zeros
from scipy.sparse import lil_matrix, linalg
from scipy.integrate import quad
from scipy.optimize import root
from physiology import Physiology
from scipy.sparse.linalg import gmres
import units
import g_output
import vascularGraph
import pdb
import run_faster
import time as ttime
import vgm

__all__ = ['LinearSystemHtdTotFixedDTBlockedE']
log = vgm.LogDispatcher.create_logger(__name__)

#------------------------------------------------------------------------------
#------------------------------------------------------------------------------


class LinearSystemHtdTotFixedDTBlockedE(object):
    """Implements and extends the discrete red blood cell transport as proposed
    by Obrist and coworkers (2010).
    """
    #@profile
    def __init__(self, G, invivo=True,dThreshold=10.0, assert_well_posedness=True,
                 init=True,**kwargs):
        """Initializes a LinearSystemHtd instance.
        INPUT: G: Vascular graph in iGraph format.
               invivo: Boolean, whether the physiological blood characteristics 
                       are calculated using the invivo (=True) or invitro (=False)
                       equations
               dThreshold: Diameter threshold below which vessels are
                           considered capillaries, to which the reordering
                           algorithm can be applied.
               assert_well_posedness: (Optional, default=True.) Boolean whether
                                      or not to check components for correct
                                      pressure boundary conditions.
               init: Assign initial conditions for RBCs (True) or keep old positions to
                        continue a simulation (False)
               **kwargs:
               ht0: The initial hematocrit in the capillary bed. If G already 
                    contains the relevant properties from an earlier simulation
                    (i.e. rRBC edge property), ht0 can be set to 'current'.
                    This will make use of the current RBC distribution as the
                    initial hematocrit
               hd0: The initial hematocrit in the capillary bed is calculated by the given
                    initial discharge hematocrit. If G already contains the relevant 
                    properties from an earlier simulation (i.e. rRBC edge property), hd0 
                    can be set to 'current'. This will make use of the current RBC 
                    distribution as the initial hematocrit
               plasmaViscosity: The dynamic plasma viscosity. If not provided,
                                a literature value will be used.
        OUTPUT: None, however the following items are created:
                self.A: Matrix A of the linear system, holding the conductance
                        information.
                self.b: Vector b of the linear system, holding the boundary
                        conditions.
        """
        self._G = G
        self._P = Physiology(G['defaultUnits'])
        self._dThreshold = dThreshold
        self._invivo=invivo
        nVertices = G.vcount()
        self._b = zeros(nVertices)
        self._x = zeros(nVertices)
        self._A = lil_matrix((nVertices,nVertices),dtype=float)
        self._eps = finfo(float).eps * 1e4
        self._tPlot = 0.0
        self._tSample = 0.0
        self._filenamelist = []
        self._timelist = []
        #self._filenamelistAvg = []
	self._timelistAvg = []
        self._sampledict = {} 
	#self._transitTimeDict = {}
	self._init=init
        self._scaleToDef=vgm.units.scaling_factor_du('mmHg',G['defaultUnits'])
        self._dtFix=0.0
        self._vertexUpdate=None
        self._edgeUpdate=None
        G.es['source']=[e.source for e in G.es]
        G.es['target']=[e.target for e in G.es]
        G.es['crosssection']=np.array([0.25*np.pi]*G.ecount())*np.array(G.es['diameter'])**2
        #Used because the Pries functions are onlt defined for vessels till 3micron
        G.es['diamCalcEff']=[i if i >= 3. else 3.0 for i in G.es['diameter'] ]
        G.es['keep_rbcs']=[[] for i in range(G.ecount())]
        adjacent=[]
        self._spacing=[]
        self._testSpacing=[]
        for i in range(G.vcount()):
            adjacent.append(G.adjacent(i))
        G.vs['adjacent']=adjacent

        htd2htt=self._P.discharge_to_tube_hematocrit
        htt2htd = self._P.tube_to_discharge_hematocrit

        if kwargs.has_key('analyzeBifEvents'):
            if kwargs['analyzeBifEvents']:
                self._analyzeBifEvents = 1
            else:
                self._analyzeBifEvents = 0
        else:
            self._analyzeBifEvents = 0
        # Assure that both pBC and rBC edge properties are present:
        for key in ['pBC', 'rBC']:
            if not G.vs[0].attributes().has_key(key):
                G.vs[0][key] = None

        if self._analyzeBifEvents:
            self._rbcsMovedPerEdge=[]
            self._edgesWithMovedRBCs=[]
            self._rbcMoveAll=[]
        else:
            if 'rbcMovedAll' in G.attributes():
                del(G['rbcMovedAll'])
            if 'rbcsMovedPerEdge' in G.attributes():
                del(G['rbcsMovedPerEdge'])
            if 'rbcMovedAll' in G.attributes():
                del(G['edgesMovedRBCs'])
        # Set initial pressure and flow to zero:
	if init:
            G.vs['pressure']=zeros(nVertices) 
            G.es['flow']=zeros(G.ecount())    
            G.vs['degree']=G.degree()
        print('Initial flow, presure, ... assigned')

        #Read sampledict (must be in folder, from where simulation is started)
        if not init:
           #self._sampledict=vgm.read_pkl('sampledict.pkl')
           self._sampledict['averagedCount']=G['averagedCount']

        #Calculate total network Volume
        G['V']=0
        for e in G.es:
            G['V']=G['V']+e['crosssection']*e['length']
        print('Total network volume calculated')

        # Compute the edge-specific minimal RBC distance:
        vrbc = self._P.rbc_volume()
        G.es['minDist'] = [vrbc / (np.pi * e['diameter']**2 / 4) for e in G.es]
        G.es['nMax'] = [np.floor(e['length']/ e['minDist']) for e in G.es] 

        # Assign capillaries and non capillary vertices
        print('Start assign capillary and non capillary vertices')
        adjacent=[np.array(G.incident(i)) for i in G.vs]
        G.vs['isCap']=[None]*G.vcount()
        self._interfaceVertices=[]
        for i in xrange(G.vcount()):
            #print(i)
            category=[]
            for j in adjacent[i]:
                #print(j)
                #print(G.es.attribute_names())
                if G.es[int(j)]['diameter'] < dThreshold:
                    category.append(1)
                else:
                    category.append(0)
            if category.count(1) == len(category):
                G.vs[i]['isCap']=True
            elif category.count(0) == len(category):
                G.vs[i]['isCap']=False
            else:
                self._interfaceVertices.append(i)
        print('End assign capillary and non capillary vertices')

        # Arterial-side inflow:
        if 'htdBC' in G.es.attribute_names():
           G.es['httBC']=[e['htdBC'] if e['htdBC'] == None else \
                self._P.discharge_to_tube_hematocrit(e['htdBC'],e['diameter'],invivo) for e in G.es()]
        if not 'httBC' in G.es.attribute_names():
            for vi in G['av']:
                for ei in G.adjacent(vi):
                    G.es[ei]['httBC'] = self._P.tube_hematocrit(
                                            G.es[ei]['diameter'], 'a')
        httBC=G.es(httBC_ne=None).indices
        #TODO Currently httBC has to be the same in all inflowEdges
        self._httBCValue=G.es[httBC[0]]['httBC']
        self._mu,self._sigma=self._compute_mu_sigma_inlet_RBC_distribution(self._httBCValue)
        print('Recheck httBC distribution')
        print(self._httBCValue)
        print(self._mu)
        print(self._sigma)

        #Convert tube hematocrit boundary condition to htdBC (in case it does not already exist)
        if not 'htdBC' in G.es.attribute_names():
           G.es['htdBC']=[e['httBC'] if e['httBC'] == None else \
                self._P.tube_to_discharge_hematocrit(e['httBC'],e['diameter'],invivo) for e in G.es()]
        print('Htt BC assigned')

        httBC_edges = G.es(httBC_ne=None).indices

        # Assign initial RBC positions:
	if init:
            if kwargs.has_key('hd0'):
                hd0=kwargs['hd0']
                if hd0 == 'current':
                    ht0=hd0
                else:
                    ht0='dummy'
            if kwargs.has_key('ht0'):
                ht0=kwargs['ht0']
            if ht0 != 'current':
                for e in G.es:
                    lrbc = e['minDist']
                    Nmax = max(int(np.floor(e['nMax'])), 1)
                    if e['httBC'] is not None:
                        N = int(np.round(e['httBC'] * Nmax))
                    else:
                        if kwargs.has_key('hd0'):
                            ht0=self._P.discharge_to_tube_hematocrit(hd0,e['diameter'],invivo)
                        N = int(np.round(ht0 * Nmax))
                    indices = sorted(np.random.permutation(Nmax)[:N])
                    e['rRBC'] = np.array(indices) * lrbc + lrbc / 2.0
                    #e['tRBC'] = np.array([])
        	    #e['path'] = np.array([])
        print('Initial nRBC computed')    
        G.es['nRBC']=[len(e['rRBC']) for e in G.es]
            
        if kwargs.has_key('plasmaViscosity'):
            self._muPlasma = kwargs['plasmaViscosity']
        else:
            self._muPlasma = self._P.dynamic_plasma_viscosity()

        # Compute nominal and specific resistance:
        self._update_nominal_and_specific_resistance()
        print('Resistance updated')

        # Compute the current tube hematocrit from the RBC positions:
        for e in G.es:
            e['htt']=e['nRBC']*vrbc/(e['crosssection']*e['length'])
            e['htd']=min(htt2htd(e['htt'], e['diameter'], invivo), 0.95)
        print('Initial htt and htd computed')        

        # This initializes the full LS. Later, only relevant parts of
        # the LS need to be changed at any timestep. Also, RBCs are
        # removed from no-flow edges to avoid wasting computational
        # time on non-functional vascular branches / fragments:
        #Convert 'pBC' ['mmHG'] to default Units
        for v in G.vs:
            if v['pBC'] != None:
                v['pBC']=v['pBC']*self._scaleToDef
        self._update_eff_resistance_and_LS(None, None, assert_well_posedness)
        print('Matrix created')
        self._solve('iterative2')
        print('Matrix solved')
        self._G.vs['pressure'] = deepcopy(self._x)
        #Convert deaultUnits to 'pBC' ['mmHG']
        for v in G.vs:
            if v['pBC'] != None:
                v['pBC']=v['pBC']/self._scaleToDef
        self._update_flow_and_velocity()
        print('Flow updated')
        self._verify_mass_balance()
        print('Mass balance verified updated')
        self._update_flow_sign()
        print('Flow sign updated')
        G.es['posFirstLast']=[None]*G.ecount()
        for i in httBC_edges:
            if len(G.es[i]['rRBC']) > 0:
                if G.es['sign'][i] == 1:
                    G.es[i]['posFirst_last']=G.es['rRBC'][i][0]
                else:
                    G.es[i]['posFirst_last']=G.es['length'][i]-G.es['rRBC'][i][-1]
            else:
                G.es[i]['posFirst_last']=G.es['length'][i]
            G.es[i]['v_last']=G.es[i]['v']
        self._update_out_and_inflows_for_vertices()
        print('updated out and inflows')
        self._update_RBCinMax()
        print('updated RBCinMax')
	
        #Calculate an estimated network turnover time (based on conditions at the beginning)
        flowsum=0
	for vi in G['av']:
            for ei in G.adjacent(vi):
                flowsum=flowsum+G.es['flow'][ei]
        G['flowSumIn']=flowsum
        G['Ttau']=G['V']/flowsum
        print(flowsum)
        print(G['V'])
        print(self._eps)
        stdout.write("\rEstimated network turnover time Ttau=%f        \n" % G['Ttau'])

        #for e in self._G.es(flow_le=self._eps*1e6):
        #    e['rRBC'] = []
    #--------------------------------------------------------------------------

    def _compute_mu_sigma_inlet_RBC_distribution(self, httBC):
        """Updates the nominal and specific resistance of a given edge 
        sequence.
        INPUT: es: Sequence of edge indices as tuple. If not provided, all 
                   edges are updated.
        OUTPUT: None, the edge properties 'resistance' and 'specificResistance'
                are updated (or created).
        """
        #mean_LD=0.28
        mean_LD=httBC
        std_LD=0.1

        #PDF log-normal
        f_x = lambda x,mu,sigma: 1./(x*np.sqrt(2*np.pi)*sigma)*np.exp(-1*(np.log(x)-mu)**2/(2*np.sigma**2))

        #PDF log-normal for line density
        f_LD = lambda z,mu,sigma: 1./((z-z**2)*np.sqrt(2*np.pi)*sigma)*np.exp(-1*(np.log(1./z-1)-mu)**2/(2*sigma**2))

        #f_mean integral dummy
        f_mean_LD_dummy = lambda z,mu,sigma: z*f_LD(z,mu,sigma)

        ##calculate mean
        f_mean_LD = lambda mu,sigma: quad(f_mean_LD_dummy,0,1,args=(mu,sigma))[0]
        f_mean_LD_Calc=np.vectorize(f_mean_LD)

        #f_var integral dummy
        f_var_LD_dummy = lambda z,mu,sigma: (z-mean_LD)**2*f_LD(z,mu,sigma)

        #calculate mean
        f_var_LD = lambda mu,sigma: quad(f_var_LD_dummy,0,1,args=(mu,sigma))[0]
        f_var_LD_Calc=np.vectorize(f_var_LD)

        #Set up system of equations
        def f_moments_LD(m):
            x,y=m
            return (f_mean_LD_Calc(x,y)-mean_LD,f_var_LD_Calc(x,y)-std_LD**2)

        optionsSolve={}
        optionsSolve['xtol']=1e-20
        if mean_LD < 0.35:
            sol=root(f_moments_LD,(0.89,0.5),method='lm',options=optionsSolve)
        else:
            sol=root(f_moments_LD,(mean_LD,std_LD),method='lm',options=optionsSolve)
        mu=sol['x'][0]
        sigma=sol['x'][1]

        return mu,sigma
            
    #--------------------------------------------------------------------------

    def _update_nominal_and_specific_resistance(self, esequence=None):
        """Updates the nominal and specific resistance of a given edge 
        sequence.
        INPUT: es: Sequence of edge indices as tuple. If not provided, all 
                   edges are updated.
        OUTPUT: None, the edge properties 'resistance' and 'specificResistance'
                are updated (or created).
        """
        G = self._G
        muPlasma=self._muPlasma
        pi=np.pi  

        if esequence is None:
            es = G.es
        else:
            es = G.es(esequence)
        es['specificResistance'] = [128 * muPlasma / (pi * d**4)
                                        for d in es['diameter']]

        es['resistance'] = [l * sr for l, sr in zip(es['length'], 
                                                es['specificResistance'])]

	self._G = G

    #--------------------------------------------------------------------------

    def _update_minDist_and_nMax(self, esequence=None):
        """Updates the length of the RBCs for each edge and the maximal Number
		of RBCs for each edge
        INPUT: es: Sequence of edge indices as tuple. If not provided, all 
                   edges are updated.
        OUTPUT: None, the edge properties 'nMax' and 'minDist'
                are updated (or created).
        """
        G = self._G

        if esequence is None:
            es = G.es
        else:
            es = G.es(esequence)
        # Compute the edge-specific minimal RBC distance:
        vrbc = self._P.rbc_volume()
        G.es['nMax'] = [np.pi * e['diameter']**2 / 4 * e['length'] / vrbc
                        for e in G.es]
        G.es['minDist'] = [e['length'] / e['nMax'] for e in G.es]

	self._G=G

    #--------------------------------------------------------------------------

    def _update_RBCinMax(self, esequence=None):
        """Updates the tube hematocrit of a given edge sequence.
        INPUT: es: Sequence of edge indices as tuple. If not provided, all 
                   edges are updated.
        OUTPUT: None, the edge property 'htt' is updated (or created).
        """
        G = self._G
        
        if esequence is None:
            es = G.es
        else:
            es = G.es(esequence)
 
        if 'RBCinMax' not in G.es.attribute_names():
            G.es['RBCinMax']=np.zeros(G.ecount())
        for e in es:
            if len(e['rRBC']) > 0:
                distToFirst=e['rRBC'][0] if e['sign']==1 else e['length']-e['rRBC'][-1]
            else:
                distToFirst=e['length']
            e['RBCinMax']=int(np.floor(distToFirst/e['minDist']))
            if e['RBCinMax']+len(e['rRBC']) > e['nMax']:
                e['RBCinMax']=int(e['nMax']-len(e['rRBC']))
	self._G=G

    #--------------------------------------------------------------------------

    def _update_hematocrit(self, esequence=None):
        """Updates the tube hematocrit of a given edge sequence.
        INPUT: es: Sequence of edge indices as tuple. If not provided, all 
                   edges are updated.
        OUTPUT: None, the edge property 'htt' is updated (or created).
        """
        G = self._G
        htt2htd = self._P.tube_to_discharge_hematocrit
        invivo=self._invivo

        if esequence is None:
            es = range(G.ecount())
        else:
            es = esequence
        for e in es:
            G.es[int(e)]['htt'] = G.es[int(e)]['nRBC'] * G.es[int(e)]['minDist'] / G.es[int(e)]['length']
            G.es[int(e)]['htd']=min(htt2htd(G.es[int(e)]['htt'], G.es[int(e)]['diameter'], invivo), 0.95)
        self._G=G

    #--------------------------------------------------------------------------

    def _update_local_pressure_gradient(self):
        """Updates the local pressure gradient at all vertices.
        INPUT: None
        OUTPUT: None, the edge property 'lpg' is updated (or created, if it did
                not exist previously)
        """
        G = self._G
#        G.es['lpg'] = np.array(G.es['specificResistance']) * \
#                      np.array(G.es['flow']) * np.array(G.es['resistance']) / \
#                      np.array(G.es['effResistance'])
        G.es['lpg'] = np.array(G.es['specificResistance']) * \
                      np.array(G.es['flow'])

        self._G=G
    #--------------------------------------------------------------------------

    def _update_interface_vertices(self):
        """(Re-)assigns each interface vertex to either the capillary or non-
        capillary group, depending on whether ist inflow is exclusively from
        capillaries or not.
        """
        G = self._G
        dThreshold = self._dThreshold

        for v in self._interfaceVerticesI:
            p = G.vs[v]['pressure']
            G.vs[v]['isCap'] = True
            for n in self._interfaceNoncapNeighborsVI[v]:
                if G.vs[n]['pressure'] > p:
                    G.vs[v]['isCap'] = False
                    break

    #--------------------------------------------------------------------------

    def _update_flow_sign(self):
        """Updates the sign of the flow. The flow is defined as having a
        positive sign if its direction is from edge source to target, negative
        if vice versa and zero otherwise (in case of equal pressures).
        INPUT: None
        OUTPUT: None (the value of the edge property 'sign' will be updated to
                one of [-1, 0, 1])
        """
        G = self._G
        if 'sign' in G.es.attributes():
            G.es['signOld']=G.es['sign']
        G.es['sign'] = [np.sign(G.vs[source]['pressure'] -
                                G.vs[target]['pressure']) for source,target in zip(G.es['source'],G.es['target'])]

    #-------------------------------------------------------------------------
    #@profile
    def _update_out_and_inflows_for_vertices(self):
        """Calculates the in- and outflow edges for vertices at the beginning.
        Afterwards in every single timestep it is check if something changed
        INPUT: None 
        OUTPUT: None, however the following parameters will be updated:
                G.vs['inflowE']: Time until next RBC reaches bifurcation.
                G.vs['outflowE']: Index of edge in which the RBC resides.
        """    
        G=self._G
        #Beginning    
        inEdges=[]
        outEdges=[]
        divergentV=[]
        convergentV=[]
        connectingV=[]
        doubleConnectingV=[]
        noFlowV=[]
        noFlowE=[]
        vertices=[]
        dThreshold = self._dThreshold
        count=0
        interfaceVertices=self._interfaceVertices
        if not 'inflowE' in G.vs.attributes():
            for v in G.vs:
                vI=v.index
                outE=[]
                inE=[]
                noFlowE=[]
                pressure = G.vs[vI]['pressure']
                adjacents=G.adjacent(vI)
                for j,nI in enumerate(G.neighbors(vI)):
                    #outEdge
                    if pressure > G.vs[nI]['pressure']: 
                        outE.append(adjacents[j])
                    elif pressure == G.vs[nI]['pressure']: 
                        noFlowE.append(adjacents[j])
                    #inflowEdge
                    else: #G.vs[vI]['pressure'] < G.vs[nI]['pressure']
                        inE.append(adjacents[j])
                        #Deal with vertices at the interface
                        #isCap is defined based on the diameter of the InflowEdge
                        if vI in interfaceVertices and G.es[adjacents[j]]['diameter'] > dThreshold:
                            G.vs[vI]['isCap']=False
                        else:
                            G.vs[vI]['isCap']=True
                #Group into divergent, convergent and connecting Vertices
                if len(outE) > len(inE) and len(inE) >= 1:
                    divergentV.append(vI)
                elif len(inE) > len(outE) and len(outE) >= 1:
                    convergentV.append(vI)
                elif len(inE) == len(outE) and len(inE) == 1:
                    connectingV.append(vI)
                elif len(inE) == len(outE) and len(inE) == 2:
                    doubleConnectingV.append(vI)
                elif vI in G['av']:
                    pass
                    #print('is Inlet Vertex')
                elif vI in G['vv']:
                    pass
                    #print('is Out Vertex')
                #print problem cases
                else:
                    for i in G.adjacent(vI):
                        if G.es['flow'][i] > 5.0e-08:
                            print('BIGERROR')
                            print(vI)
                            print(inE)
                            print(outE)
                            print(noFlowE)
                            print(i)
                            print('Flow and diameter')
                            print(G.es['flow'][i])
                            print(G.es['diameter'][i])
                        noFlowE.append(i)
                    inE=[]
                    outE=[]
                    noFlowV.append(vI)
                inEdges.append(inE)
                outEdges.append(outE)
            G.vs['inflowE']=inEdges
            G.vs['outflowE']=outEdges
            G.es['noFlow']=[0]*G.ecount()
            noFlowE=np.unique(noFlowE)
            G.es[noFlowE]['noFlow']=[1]*len(noFlowE)
            G['divV']=divergentV
            G['conV']=convergentV
            G['connectV']=connectingV
            G['dConnectV']=doubleConnectingV
            G['noFlowV']=noFlowV
            print('assign vertex types')
            #vertex type av = 1, vv = 2,divV = 3, conV = 4, connectV = 5, dConnectV = 6, noFlowV = 7
            G.vs['vType']=[0]*G.vcount()
            for i in G['av']:
                G.vs[i]['vType']=1
            for i in G['vv']:
                G.vs[i]['vType']=2
            for i in G['divV']:
                G.vs[i]['vType']=3
            for i in G['conV']:
                G.vs[i]['vType']=4
            for i in G['connectV']:
                G.vs[i]['vType']=5
            for i in G['dConnectV']:
                G.vs[i]['vType']=6
            for i in G['noFlowV']:
                G.vs[i]['vType']=7
            if len(G.vs(vType_eq=0).indices) > 0:
                print('BIGERROR vertex type not assigned')
            del(G['divV'])
            del(G['conV'])
            del(G['connectV'])
            del(G['dConnectV'])
        #Every Time Step
        else:
            print('Update_Out_and_inflows')
            if G.es['sign']!=G.es['signOld']:
                sign=np.array(G.es['sign'])
                signOld=np.array(G.es['signOld'])
                sumTes=abs(sign+signOld)
                #find edges where sign change took place
                edgeList=np.array(np.where(sumTes < abs(2))[0])
                edgeList=edgeList.tolist()
                sign0=G.es(sign_eq=0,signOld_eq=0).indices
                for e in sign0:
                    edgeList.remove(e)
                stdout.flush()
                vertices=[]
                for e in edgeList:
                    for vI in G.es[int(e)].tuple:
                        vertices.append(vI)
                vertices=np.unique(vertices)
                count = 0
                for vI in vertices:
                    #vI=v.index
                    count += 1
                    vI=int(vI)
                    outE=[]
                    inE=[]
                    noFlowE=[]
                    neighbors=G.neighbors(vI)
                    pressure = G.vs[vI]['pressure']
                    adjacents=G.adjacent(vI)
                    for j in range(len(neighbors)):
                        nI=neighbors[j]
                        #outEdge
                        if pressure > G.vs[nI]['pressure']:
                            outE.append(adjacents[j])
                        elif pressure == G.vs[nI]['pressure']:
                            noFlowE.append(adjacents[j])
                        #inflowEdge
                        else: #G.vs[vI]['pressure'] < G.vs[nI]['pressure']
                            inE.append(adjacents[j])
                            #Deal with vertices at the interface
                            #isCap is defined based on the diameter of the InflowEdge
                            if vI in interfaceVertices and G.es[adjacents[j]]['diameter'] > dThreshold:
                                G.vs[vI]['isCap']=False
                            else:
                                G.vs[vI]['isCap']=True
                    #Group into divergent, convergent, connecting, doubleConnecting and noFlow Vertices
                    #it is now a divergent Vertex
                    if len(outE) > len(inE) and len(inE) >= 1:
                        #Find history of vertex
                        if G.vs[vI]['vType']==7:
                            G.es[inE]['noFlow']=[0]*len(inE)
                            G.es[outE]['noFlow']=[0]*len(outE)
                        G.vs[vI]['vType']=3
                        G.vs[vI]['inflowE']=inE
                        G.vs[vI]['outflowE']=outE
                    #it is now a convergent Vertex
                    elif len(inE) > len(outE) and len(outE) >= 1:
                        if G.vs[vI]['vType']==7:
                            G.es[inE]['noFlow']=[0]*len(inE)
                            G.es[outE]['noFlow']=[0]*len(outE)
                        G.vs[vI]['vType']=4
                        G.vs[vI]['inflowE']=inE
                        G.vs[vI]['outflowE']=outE
                    #it is now a connecting Vertex
                    elif len(outE) == len(inE) and len(outE) == 1:
                        #print(' ')
                        if G.vs[vI]['vType']==7:
                            G.es[inE]['noFlow']=[0]*len(inE)
                            G.es[outE]['noFlow']=[0]*len(outE)
                        G.vs[vI]['vType']=5
                        G.vs[vI]['inflowE']=inE
                        G.vs[vI]['outflowE']=outE
                    #it is now a double connecting Vertex
                    elif len(outE) == len(inE) and len(outE) == 2:
                        if G.vs[vI]['vType']==7:
                            G.es[inE]['noFlow']=[0]*len(inE)
                            G.es[outE]['noFlow']=[0]*len(outE)
                        G.vs[vI]['vType']=6
                        G.vs[vI]['inflowE']=inE
                        G.vs[vI]['outflowE']=outE
                    elif vI in G['av']:
                        if G.vs[vI]['rBC'] != None:
                            for j in G.adjacent(vI):
                                if G.es[j]['flow'] > 1e-6:
                                    print(' ')
                                    print(vI)
                                    print(len(G.vs[vI]['inflowE']))
                                    print(len(G.vs[vI]['outflowE']))
                                    print('ERROR flow direction of inlet vertex changed')
                                    print(G.es[G.adjacent(vI)]['flow'])
                                    print(G.vs[vI]['rBC'])
                                    print(G.vs[vI]['kind'])
                                    print(G.vs[vI]['isSrxtm'])
                                    print(G.es[G.adjacent(vI)]['sign'])
                                    print(G.es[G.adjacent(vI)]['signOld'])
                        else:
                            print('WARNING direction out av changed to vv')
                            print(vI)
                            G.vs[vI]['av'] = 0
                            G.vs[vI]['vv'] = 1
                            G.vs[vI]['vType'] = 2
                            edgeVI=G.adjacent(vI)[0]
                            G.es[edgeVI]['httBC']=None
                            G.es[edgeVI]['posFirst_last']=None
                            G.es[edgeVI]['v_last']=None
                    elif vI in G['vv']:
                        if G.vs[vI]['rBC'] != None:
                            for j in G.adjacent(vI):
                                if G.es[j]['flow'] > 1e-6:
                                    print(' ')
                                    print(vI)
                                    print(len(G.vs[vI]['inflowE']))
                                    print(len(G.vs[vI]['outflowE']))
                                    print('ERROR flow direction of out vertex changed')
                                    print(G.es[G.adjacent(vI)]['flow'])
                                    print(G.vs[vI]['rBC'])
                                    print(G.vs[vI]['kind'])
                                    print(G.vs[vI]['isSrxtm'])
                                    print(G.es[G.adjacent(vI)]['sign'])
                                    print(G.es[G.adjacent(vI)]['signOld'])
                        else:
                            print('WARNING direction out vv changed to av')
                            print(vI)
                            G.vs[vI]['av'] = 1
                            G.vs[vI]['vv'] = 0
                            G.vs[vI]['vType'] = 1
                            edgeVI=G.adjacent(vI)[0]
                            G.es[edgeVI]['httBC']=self._httBCValue
                            if len(G.es[edgeVI]['rRBC']) > 0:
                                if G.es['sign'][edgeVI] == 1:
                                    G.es[edgeVI]['posFirst_last']=G.es['rRBC'][edgeVI][0]
                                else:
                                    G.es[edgeVI]['posFirst_last']=G.es['length'][edgeVI]-G.es['rRBC'][edgeVI][-1]
                            else:
                                G.es[edgeVI]['posFirst_last']=G.es['length'][edgeVI]
                            G.es[edgeVI]['v_last']=G.es[edgeVI]['v']
                    #it is now a noFlow Vertex
                    else:
                        noFlowEdges=[]
                        for i in G.adjacent(vI):
                            if G.es['flow'][i] > 5.0e-08:
                                print('BIGERROR')
                                print(vI)
                                print(inE)
                                print(outE)
                                print(noFlowE)
                                print(i)
                                print('Flow and diameter')
                                print(G.es['flow'][i])
                                print(G.es['diameter'][i])
                            noFlowEdges.append(i)
                        G.vs[vI]['vType']=7
                        G.es[noFlowEdges]['noFlow']=[1]*len(noFlowEdges)
                        G.vs[vI]['inflowE']=[]
                        G.vs[vI]['outflowE']=[]
        G['av']=G.vs(av_eq=1).indices
        G['vv']=G.vs(vv_eq=1).indices
        stdout.flush()
    #--------------------------------------------------------------------------

    def _update_flow_and_velocity(self):
        """Updates the flow and red blood cell velocity in all vessels
        INPUT: None
        OUTPUT: None
        """

        G = self._G
        invivo=self._invivo
        vf = self._P.velocity_factor
        vrbc = self._P.rbc_volume()
        vfList=[1.0 if htt == 0.0 else max(1.0,vf(d, invivo, tube_ht=htt)) for d,htt in zip(G.es['diameter'],G.es['htt'])]

        self._G=run_faster.update_flow_and_v(self._G,self._invivo,vfList,vrbc)
        G= self._G

        #G = self._G
        #invivo=self._invivo
        #vf = self._P.velocity_factor
        #pi=np.pi
        #G.es['flow'] = np.array([abs(G.vs[e.source]['pressure'] -                                           
        #                    G.vs[e.target]['pressure']) /res                        
        #                    for e,res in zip(G.es,G.es['effResistance'])])
        # RBC velocity is not defined if tube_ht==0, using plasma velocity
        # instead:
        #G.es['v'] = [4 * flow * vf(d, invivo, tube_ht=htt) /                  
        #            (pi * d**2) if htt > 0 else                                
        #            4 * flow / (pi * d**2)                                     
         #           for flow,d,htt in zip(G.es['flow'],G.es['diameter'],G.es['htt'])]


    #--------------------------------------------------------------------------

    def _update_eff_resistance_and_LS(self, newGraph=None, vertex=None,
                                      assert_well_posedness=True):
        """Constructs the linear system A x = b where the matrix A contains the
        conductance information of the vascular graph, the vector b specifies
        the boundary conditions and the vector x holds the pressures at the
        vertices (for which the system needs to be solved). x will have the
        same units of [pressure] as the pBC vertices.

        Note that in this approach, A and b contain a mixture of dimensions,
        i.e. A and b have dimensions of [1.0] and [pressure] in the pBC case,
        [conductance] and [conductance*pressure] otherwise, the latter being
        rBCs. This has the advantage that no re-indexing is required as the
        matrices contain all vertices.
        INPUT: newGraph: Vascular graph in iGraph format to replace the
                         previous self.G. (Optional, default=None.)
               assert_well_posedness: (Optional, default=True.) Boolean whether
                                      or not to check components for correct
                                      pressure boundary conditions.
        OUTPUT: A: Matrix A of the linear system, holding the conductance
                   information.
                b: Vector b of the linear system, holding the boundary
                   conditions.
        """

        #if newGraph is not None:
        #    self._G = newGraph

        G = self._G
        P = self._P
        A = self._A
        b = self._b
        x = self._x
        invivo = self._invivo

        htt2htd = P.tube_to_discharge_hematocrit
        nurel = P.relative_apparent_blood_viscosity
        if assert_well_posedness:
            # Ensure that the problem is well posed in terms of BCs.
            # This takes care of unconnected nodes as well as connected
            # components of the graph that have not been assigned a minimum of
            # one pressure boundary condition:
            for component in G.components():
                if all([x is None for x in G.vs(component)['pBC']]):
                    i = component[0]
                    G.vs[i]['pBC'] = 0.0

        if vertex is None:
            vertexList = range(G.vcount())
            edgeList = range(G.ecount())
        else:
            vertexList=[]
            edgeList=[]
            for i in vertex:
                vList = np.concatenate([[i],
                     G.neighbors(i)]).tolist()
                eList = G.adjacent(i)
                vertexList=np.concatenate([vertexList,vList]).tolist()
                edgeList=np.concatenate([edgeList,eList]).tolist()
            vertexList=np.unique(vertexList).tolist()
            edgeList=np.unique(edgeList).tolist()
            edgeList=[int(i) for i in edgeList]
            vertexList=[int(i) for i in vertexList]
        dischargeHt = [min(htt2htd(e, d, invivo), 0.95) for e,d in zip(G.es[edgeList]['htt'],G.es[edgeList]['diameter'])]
        G.es[edgeList]['effResistance'] =[ res * nurel(d, dHt,invivo) for res,dHt,d in zip(G.es[edgeList]['resistance'], \
            dischargeHt,G.es[edgeList]['diamCalcEff'])]

        edgeList = G.es(edgeList)
        vertexList = G.vs(vertexList)
        for vertex in vertexList:
            i = vertex.index
            A.data[i] = []
            A.rows[i] = []
            b[i] = 0.0
            if vertex['pBC'] is not None:
                A[i,i] = 1.0
                b[i] = vertex['pBC']
            else:
                aDummy=0
                k=0
                neighbors=[]
                for edge in G.adjacent(i,'all'):
                    if G.is_loop(edge):
                        continue
                    j=G.neighbors(i)[k]
                    k += 1
                    conductance = 1 / G.es[edge]['effResistance']
                    neighbor = G.vs[j]
                    # +=, -= account for multiedges
                    aDummy += conductance
                    if neighbor['pBC'] is not None:
                        b[i] = b[i] + neighbor['pBC'] * conductance
                    #elif neighbor['rBC'] is not None:
                     #   b[i] = b[i] + neighbor['rBC']
                    else:
                        if j not in neighbors:
                            A[i,j] = - conductance
                        else:
                            A[i,j] = A[i,j] - conductance
                    neighbors.append(j)
                    if vertex['rBC'] is not None:
                        b[i] += vertex['rBC']
                A[i,i]=aDummy

        self._A = A
        self._b = b
        self._G = G

    #--------------------------------------------------------------------------
    #@profile
    def _propagate_rbc(self):
        """This assigns the current bifurcation-RBC to a new edge and
        propagates all RBCs until the next RBC reaches at a bifurcation.
        INPUT: None
        OUTPUT: None
        """
        G = self._G
        dt = self._dt # Time to propagate RBCs with current velocity.
        eps=self._eps
        #No flow Edges are not considered for the propagation of RBCs
        edgeList=G.es(noFlow_eq=0).indices
        if self._analyzeBifEvents:
            rbcsMovedPerEdge=[]
            edgesWithMovedRBCs=[]
            rbcMoved = 0
        edgeList=G.es[edgeList]
        #TODO sorting should not be needed anymore
        #pOut=[G.vs[e['target']]['pressure'] if e['sign'] == 1.0 else G.vs[e['source']]['pressure']
        #    for e in edgeList]
        #sortedE=zip(pOut,range(len(edgeList)))
        #sortedE.sort()
        #sortedE=[i[1] for i in sortedE]

        convEdges2=[]
        edgeUpdate=[]   #Edges where the number of RBCs changed --> need to be updated
        vertexUpdate=[] #Vertices where the number of RBCs changed in adjacent edges --> need to be updated

        print('Total number of Edges')
        print(len(edgeList))
        for e in edgeList:
            print('Edge currently analyzing')
            print(e.index)
            stdout.flush()
            edgesInvolved=[] #all edges connected to the bifurcation vertex
            sign=e['sign']
            ei=e.index
            #Get bifurcation vertex
            if sign == 1:
                vi=e.target
            else:
                vi=e.source
            for i in G.vs[vi]['inflowE']:
                 edgesInvolved.append(i)
            for i in G.vs[vi]['outflowE']:
                 edgesInvolved.append(i)
            overshootsNo=0 #Reset - Number of overshoots acutally taking place (considers possible number of bifurcation events)
            boolHttEdge=0
            boolHttEdge2=0
            boolHttEdge3=0
            if ei not in convEdges2 and G.vs[vi]['vType'] != 7:
                #If there is a BC for the edge new RBCs have to be introduced
                if e['httBC'] is not None:
                    boolHttEdge = 1
                    rRBC = []
                    lrbc = e['minDist']
                    htt = e['httBC']
                    length = e['length']
                    nMaxNew=e['nMax']-len(e['rRBC'])
                    if len(e['rRBC']) > 0:
                        #if cum_length > distToFirst:
                        posFirst=e['rRBC'][0] if e['sign']==1.0 else e['length']-e['rRBC'][-1]
                        cum_length = posFirst
                    else:
                        cum_length = e['posFirst_last'] + e['v_last'] * dt
                        posFirst = cum_length
                    while cum_length >= lrbc and nMaxNew > 0:
                        if len(e['keep_rbcs']) != 0:
                            if posFirst - e['keep_rbcs'][0] >= 0:
                                rRBC.append(posFirst - e['keep_rbcs'][0])
                                nMaxNew += -1
                                posFirst=posFirst - e['keep_rbcs'][0]
                                cum_length = posFirst
                                e['keep_rbcs']=[]
                                e['posFirst_last']=posFirst
                                e['v_last']=e['v']
                            else:
                                if len(e['rRBC']) > 0:
                                    e['posFirst_last'] = posFirst
                                    e['v_last']=e['v']
                                else:
                                    e['posFirst_last'] += e['v_last'] * dt
                                break
                        else:
                            #number of RBCs randomly chosen to average htt
                            number=np.exp(self._mu+self._sigma*np.random.randn(1)[0])
                            #self._spacing.append(number)
                            spacing = lrbc+lrbc*number
                            if posFirst - spacing >= 0:
                                rRBC.append(posFirst - spacing)
                                nMaxNew += -1
                                posFirst=posFirst - spacing
                                cum_length = posFirst
                                e['posFirst_last']=posFirst
                                e['v_last']=e['v']
                            else:
                                e['keep_rbcs']=[spacing]
                                e['v_last']=e['v']
                                if len(rRBC) == 0:
                                    e['posFirst_last']=posFirst
                                else:
                                    e['posFirst_last']=rRBC[-1]
                                break
                    rRBC = np.array(rRBC)
                    if len(rRBC) >= 1.:
                        if e['sign'] == 1:
                            e['rRBC'] = np.concatenate([rRBC[::-1], e['rRBC']])
                            vertexUpdate.append(e.target)
                            vertexUpdate.append(e.source)
                            edgeUpdate.append(ei)
                        else:
                            e['rRBC'] = np.concatenate([e['rRBC'], length-rRBC])
                            vertexUpdate.append(e.target)
                            vertexUpdate.append(e.source)
                            edgeUpdate.append(ei)
            #Check if the RBCs in the edge have been moved already (--> convergent bifurcation)
            #Recheck if bifurcation vertex is a noFlow Vertex (vType=7)
            #if ei not in convEdges2 and G.vs[vi] != 7:
	        #If RBCs are present move all RBCs
                if len(e['rRBC']) > 0:
                    e['rRBC'] = e['rRBC'] + e['v'] * dt * e['sign']
                    #Deal with bifurcation events and overshoots in every edge
                    #bifRBCsIndes - array with overshooting RBCs from smallest to largest index
                    bifRBCsIndex=[]
                    nRBC=len(e['rRBC'])
                    if sign == 1.0:
                        if e['rRBC'][-1] > e['length']:
                            for i,j in enumerate(e['rRBC'][::-1]):
                                if j > e['length']:
                                    bifRBCsIndex.append(nRBC-1-i)
                                else:
                                    break
                        bifRBCsIndex=bifRBCsIndex[::-1]
                    else:
                        if e['rRBC'][0] < 0:
                            for i,j in enumerate(e['rRBC']):
                                if j < 0:
                                    bifRBCsIndex.append(i)
                                else:
                                    break
                    noBifEvents=len(bifRBCsIndex)
                else:
                    noBifEvents = 0
                #Convergent Edge without a bifurcation event
                if noBifEvents == 0 and G.vs[vi]['vType']==4:
                    convEdges2.append(ei)
        #-------------------------------------------------------------------------------------------
                #Check if a bifurcation event is taking place
                if noBifEvents > 0:
                    #If Edge is outlflow Edge, simply remove RBCs
                    if G.vs[vi]['vType'] == 2:
                        overshootsNo=noBifEvents
                        print('at outflow')
                        e['rRBC']=e['rRBC'][:-noBifEvents] if sign == 1.0 else e['rRBC'][noBifEvents::]
                        vertexUpdate.append(e.target)
                        vertexUpdate.append(e.source)
                        edgeUpdate.append(ei)
        #-------------------------------------------------------------------------------------------
                    #if vertex is connecting vertex
                    elif G.vs[vi]['vType'] == 5:
                        print('at connecting vertex')
                        if len(e['rRBC']) >= 2:
                            for i in range(len(e['rRBC'])-1):
                                if e['rRBC'][i+1]-e['rRBC'][i] + eps < e['minDist']:
                                    print('BIG ERROR START')
                                    print(e['rRBC'][i+1]-e['rRBC'][i])
                                    print(e['minDist'])
                                    print(e['rRBC'][i+1])
                                    print(e['rRBC'][i])
                        outE=G.vs[vi]['outflowE'][0]
                        oe=G.es[outE]
                        if noBifEvents <= oe['RBCinMax']:
                            overshootsNo=int(noBifEvents)
                            posBifRBCsIndex = bifRBCsIndex
                        else:
                            overshootsNo=int(oe['RBCinMax'])
                            posBifRBCsIndex=[bifRBCsIndex[-overshootsNo::] if sign == 1.0 \
                                else bifRBCsIndex[:overshootsNo]]
                        if overshootsNo > 0:
                            #overshootDist starts with RBC which overshoots the least and goes larges overshoot
                            overshootDist=e['rRBC'][posBifRBCsIndex]-[e['length']]*overshootsNo if sign == 1.0 \
                                else [0]*overshootsNo-e['rRBC'][posBifRBCsIndex]
                            if sign != 1:
                                overshootDist = overshootDist[::-1]
                            overshootTime=overshootDist / ([e['v']]*overshootsNo)
                            position=np.array(overshootTime)*np.array([oe['v']]*overshootsNo)
                            position=self._check_new_RBC_position(oe,position,overshootsNo)
                            #Add rbcs to new Edge       
                            if oe['sign'] == 1.0:
                                oe['rRBC']=np.concatenate([position, oe['rRBC']])
                            else:
                                position = [oe['length']]*overshootsNo - position[::-1]
                                oe['rRBC']=np.concatenate([oe['rRBC'],position])
                            #Remove RBCs from old Edge
                            if sign == 1.0:
                                e['rRBC']=e['rRBC'][:-overshootsNo]
                            else:
                                e['rRBC']=e['rRBC'][overshootsNo::]
                        #Deal with RBCs which could not be reassigned to the new edge because of a traffic jam
                        noStuckRBCs=len(bifRBCsIndex)-overshootsNo
                        if noStuckRBCs >0:
                            e['rRBC']=self._reposition_stuck_RBCs(e,noStuckRBCs)
       #-------------------------------------------------------------------------------------------
                    #if vertex is divergent vertex
                    elif G.vs[vi]['vType'] == 3:
                        print('is at divergent vertex')
                        outEdges=G.vs[vi]['outflowE']
		        #Differ between capillaries and non-capillaries
                        if G.vs[vi]['isCap']:
                            preferenceList = [x[1] for x in \
                                sorted(zip(np.array(G.es[outEdges]['flow'])/np.array(G.es[outEdges]['crosssection']), \
                                outEdges), reverse=True)]
                        else:
                            preferenceList = [x[1] for x in sorted(zip(G.es[outEdges]['flow'], outEdges), reverse=True)]
			#Define prefered OutEdges
                        outEPref=preferenceList[0]
                        oe=G.es[outEPref]
                        outEPref2=preferenceList[1]
                        oe2=G.es[outEPref2]
                        if len(outEdges) > 2:
                            outEPref3=preferenceList[2]
                            oe3=G.es[outEPref3]
			#Check bifurcation events taking place
                        if noBifEvents <= oe['RBCinMax']:
                            overshootsNo=int(noBifEvents)
                            overshootsNo2=0
                            overshootsNo3=0
                        else:
                            overshootsNo=oe['RBCinMax']
                            if noBifEvents-overshootsNo < oe2['RBCinMax']:
                                overshootsNo2=int(noBifEvents-overshootsNo)
                                overshootsNo3=0
                            else:
                                overshootsNo2 = int(oe2['RBCinMax'])
                                if len(outEdges) > 2:
                                    if noBifEvents-overshootsNo-overshootsNo2 < oe3['RBCinMax']:
                                        overshootsNo3 = int(noBifEvents-overshootsNo-overshootsNo2)
                                    else:
                                        overshootsNo3 = int(oe3['RBCinMax'])
                                else:
                                    overshootsNo3 = 0
                        overshootsNoTot=int(overshootsNo+overshootsNo2+overshootsNo3)
                        if overshootsNoTot > 0:
                            overshootDist=[e['rRBC'][bifRBCsIndex[-1*overshootsNoTot::]]-[e['length']]*overshootsNoTot if sign == 1.0     
                                else [0]*overshootsNoTot-e['rRBC'][bifRBCsIndex[0:overshootsNoTot]]][0]
                            if sign != 1:
                                overshootDist = overshootDist[::-1]
                            overshootTime=overshootDist / ([e['v']]*overshootsNoTot)
                        #TODO currently the first set of arriving RBCs is put into Pref1 and there spacing is adjusted
                        #second set ist put into Pref2 and so on...
                        if overshootsNo > 0:
                            position=np.array(overshootTime[-1*overshootsNo::])*np.array([oe['v']]*overshootsNo)
                            position=self._check_new_RBC_position(oe,position,overshootsNo)
                            #Add rbcs to new Edge       
                            if oe['sign'] == 1.0:
                                oe['rRBC']=np.concatenate([position, oe['rRBC']])
                            else:
                                position = [oe['length']]*overshootsNo - position[::-1]
                                oe['rRBC']=np.concatenate([oe['rRBC'],position])
                        #Calculate position of RBCs in prefered outEdge2
                        if overshootsNo2 > 0:
                            if overshootsNo > 0:
                                position2=np.array(overshootTime[-1*(overshootsNo+overshootsNo2):-1*overshootsNo:]) \
                                    *np.array([oe2['v']]*overshootsNo2)
                            else:
                                position2=np.array(overshootTime[-1*(overshootsNo2)::])*np.array([oe2['v']]*overshootsNo2)
                            position2=self._check_new_RBC_position(oe2,position2,overshootsNo2)
                            #Add rbcs to new Edge       
                            if oe2['sign'] == 1.0:
                                oe2['rRBC']=np.concatenate([position2, oe2['rRBC']])
                            else:
                                position2 = [oe2['length']]*overshootsNo2 - position2[::-1]
                                oe2['rRBC']=np.concatenate([oe2['rRBC'],position2])
                        if len(outEdges) > 2:
                            if overshootsNo3 > 0:
                                #Calculate position of RBCs in prefered outEdge3
                                if overshootsNo == 0 and overshootsNo2 == 0:
                                    position3=np.array(overshootTime[-1*(overshootsNo3)::])*np.array([oe3['v']] \
                                        *overshootsNo3)
                                else:
                                    position3=np.array(overshootTime[-1*(overshootsNo+overshootsNo2+overshootsNo3):\
                                        -1*(overshootsNo+overshootsNo2):])*np.array([oe3['v']]*overshootsNo3)
                                position3=self._check_new_RBC_position(oe3,position3,overshootsNo3)
                                #Add rbcs to new Edge       
                                if oe3['sign'] == 1.0:
                                    oe3['rRBC']=np.concatenate([position3, oe3['rRBC']])
                                else:
                                    position3 = [oe3['length']]*overshootsNo3 - position3[::-1]
                                    oe3['rRBC']=np.concatenate([oe3['rRBC'],position3])
                        #Remove RBCs from old Edge
                        if overshootsNoTot > 0:
                            if sign == 1.0:
                                e['rRBC']=e['rRBC'][:-overshootsNoTot]
                            else:
                                e['rRBC']=e['rRBC'][overshootsNoTot::]
                        #Deal with RBCs which could not be reassigned to the new edge because of a traffic jam
                        noStuckRBCs=len(bifRBCsIndex)-overshootsNoTot
                        if noStuckRBCs >0:
                            e['rRBC']=self._reposition_stuck_RBCs(e,noStuckRBCs)
        #-------------------------------------------------------------------------------------------
                #if vertex is convergent vertex
                    elif G.vs[vi]['vType'] == 4:
                        print('it is a convergent bifurcation')
                        bifRBCsIndex1=bifRBCsIndex
                        noBifEvents1=noBifEvents
                        outE=G.vs[vi]['outflowE'][0]
                        oe=G.es[outE]
                        inflowEdges=G.vs[vi]['inflowE']
                        k=0
                        for i in inflowEdges:
                            if i == e.index:
                                inE1=e.index
                            else:
                                if k == 0:
                                    inE2=i
                                    e2=G.es[inE2]
                                    k = 1
                                else:
                                  inE3=i
                                  e3=G.es[inE3]
                        #Move RBCs in second inEdge (if that has not been done already)
                        if inE2 not in convEdges2:
                            convEdges2.append(inE2)
                            #Check if httBC exists
                            boolHttEdge2 = 0
                            if e2['httBC'] is not None:
                                boolHttEdge2 = 1
                                rRBC = []
                                lrbc = e2['minDist']
                                htt = e2['httBC']
                                length = e2['length']
                                nMaxNew=e2['nMax']-len(e2['rRBC'])
                                if len(e2['rRBC']) > 0:
                                    #if cum_length > distToFirst:
                                    posFirst=e2['rRBC'][0] if e2['sign']==1.0 else e2['length']-e2['rRBC'][-1]
                                    cum_length = posFirst
                                else:
                                    cum_length = e2['posFirst_last'] + e2['v_last'] * dt
                                    posFirst = cum_length
                                while cum_length >= lrbc and nMaxNew > 0:
                                    if len(e2['keep_rbcs']) != 0:
                                        if posFirst - e2['keep_rbcs'][0] >= 0:
                                            rRBC.append(posFirst - e2['keep_rbcs'][0])
                                            nMaxNew += -1
                                            posFirst=posFirst - e2['keep_rbcs'][0]
                                            cum_length = posFirst
                                            e2['keep_rbcs']=[]
                                            e2['posFirst_last']=posFirst
                                            e2['v_last']=e['v']
                                        else:
                                            if len(e2['rRBC']) > 0:
                                                e2['posFirst_last'] = posFirst
                                                e2['v_last']=e2['v']
                                            else:
                                                e2['posFirst_last'] += e2['v_last'] * dt
                                            break
                                    else:
                                        #number of RBCs randomly chosen to average htt
                                        number=np.exp(self._mu+self._sigma*np.random.randn(1)[0])
                                        #self._spacing.append(number)
                                        spacing = lrbc+lrbc*number
                                        if posFirst - spacing >= 0:
                                            rRBC.append(posFirst - spacing)
                                            nMaxNew += -1
                                            posFirst=posFirst - spacing
                                            cum_length = posFirst
                                            e2['posFirst_last']=posFirst
                                            e2['v_last']=e2['v']
                                        else:
                                            e2['keep_rbcs']=[spacing]
                                            e2['v_last']=e2['v']
                                            if len(rRBC) == 0:
                                                e2['posFirst_last']=posFirst
                                            else:
                                                e2['posFirst_last']=rRBC[-1]
                                            break
                                rRBC = np.array(rRBC)
                                if len(rRBC) >= 1.:
                                    if e2['sign'] == 1:
                                        e2['rRBC'] = np.concatenate([rRBC[::-1], e2['rRBC']])
                                        vertexUpdate.append(e2.target)
                                        vertexUpdate.append(e2.source)
                                        edgeUpdate.append(e2.index)
                                    else:
                                        e2['rRBC'] = np.concatenate([e2['rRBC'], length-rRBC])
                                        vertexUpdate.append(e2.target)
                                        vertexUpdate.append(e2.source)
                                        edgeUpdate.append(e2.index)
                            #Check if httBC exists
                            #If RBCs are present move all RBCs in inEdge2
                            if len(e2['rRBC']) > 0:
                                e2['rRBC'] = e2['rRBC'] + e2['v'] * dt * e2['sign']
                                bifRBCsIndex2=[]
                                nRBC2=len(e2['rRBC'])
                                if e2['sign'] == 1.0:
                                    if e2['rRBC'][-1] > e2['length']:
                                        for i,j in enumerate(e2['rRBC'][::-1]):
                                            if j > e2['length']:
                                                bifRBCsIndex2.append(nRBC2-1-i)
                                            else:
                                                break
                                    bifRBCsIndex2=bifRBCsIndex2[::-1]
                                else:
                                    if e2['rRBC'][0] < 0:
                                        for i,j in enumerate(e2['rRBC']):
                                            if j < 0:
                                                bifRBCsIndex2.append(i)
                                            else:
                                                break
                                noBifEvents2=len(bifRBCsIndex2)
                            else:
                                bifRBCsIndex2=[]
                                noBifEvents2=0
                            sign2=e2['sign']
                        else:
                            noBifEvents2=0
                            bifRBCsIndex2=[]
                            sign2=e2['sign']
                        #Check if there is a third inEdge
                        if len(inflowEdges) > 2:
                            e3=G.es[inE3]
                            if inE3 not in convEdges2:
                                convEdges2.append(inE3)
                                #Check if httBC exists
                                boolHttEdge3 = 0
                                if e3['httBC'] is not None:
                                    boolHttEdge3 = 1
                                    rRBC = []
                                    lrbc = e3['minDist']
                                    htt = e3['httBC']
                                    length = e3['length']
                                    nMaxNew=e3['nMax']-len(e3['rRBC'])
                                    if len(e3['rRBC']) > 0:
                                        #if cum_length > distToFirst:
                                        posFirst=e3['rRBC'][0] if e3['sign']==1.0 else e3['length']-e3['rRBC'][-1]
                                        cum_length = posFirst
                                    else:
                                        cum_length = e3['posFirst_last'] + e3['v_last'] * dt
                                        posFirst = cum_length
                                    while cum_length >= lrbc and nMaxNew > 0:
                                        if len(e3['keep_rbcs']) != 0:
                                            if posFirst - e3['keep_rbcs'][0] >= 0:
                                                rRBC.append(posFirst - e3['keep_rbcs'][0])
                                                nMaxNew += -1
                                                posFirst=posFirst - e3['keep_rbcs'][0]
                                                cum_length = posFirst
                                                e3['keep_rbcs']=[]
                                                e3['posFirst_last']=posFirst
                                                e3['v_last']=e['v']
                                            else:
                                                if len(e3['rRBC']) > 0:
                                                    e3['posFirst_last'] = posFirst
                                                    e3['v_last']=e3['v']
                                                else:
                                                    e3['posFirst_last'] += e3['v_last'] * dt
                                                break
                                        else:
                                            #number of RBCs randomly chosen to average htt
                                            number=np.exp(self._mu+self._sigma*np.random.randn(1)[0])
                                            #self._spacing.append(number)
                                            spacing = lrbc+lrbc*number
                                            if posFirst - spacing >= 0:
                                                rRBC.append(posFirst - spacing)
                                                nMaxNew += -1
                                                posFirst=posFirst - spacing
                                                cum_length = posFirst
                                                e3['posFirst_last']=posFirst
                                                e3['v_last']=e3['v']
                                            else:
                                                e3['keep_rbcs']=[spacing]
                                                e3['v_last']=e3['v']
                                                if len(rRBC) == 0:
                                                    e3['posFirst_last']=posFirst
                                                else:
                                                    e3['posFirst_last']=rRBC[-1]
                                                break
                                    rRBC = np.array(rRBC)
                                    if len(rRBC) >= 1.:
                                        if e3['sign'] == 1:
                                            e3['rRBC'] = np.concatenate([rRBC[::-1], e3['rRBC']])
                                            vertexUpdate.append(e3.target)
                                            vertexUpdate.append(e3.source)
                                            edgeUpdate.append(e3.index)
                                        else:
                                            e3['rRBC'] = np.concatenate([e3['rRBC'], length-rRBC])
                                            vertexUpdate.append(e3.target)
                                            vertexUpdate.append(e3.source)
                                            edgeUpdate.append(e3.index)
                                #If RBCs are present move all RBCs in inEdge3
                                if len(e3['rRBC']) > 0:
                                    e3['rRBC'] = e3['rRBC'] + e3['v'] * dt * e3['sign']
                                    bifRBCsIndex3=[]
                                    nRBC3=len(e3['rRBC'])
                                    if e3['sign'] == 1.0:
                                        if e3['rRBC'][-1] > e3['length']:
                                            for i,j in enumerate(e3['rRBC'][::-1]):
                                                if j > e3['length']:
                                                    bifRBCsIndex3.append(nRBC3-1-i)
                                                else:
                                                    break
                                        bifRBCsIndex3=bifRBCsIndex3[::-1]
                                    else:
                                        if e3['rRBC'][0] < 0:
                                            for i,j in enumerate(e3['rRBC']):
                                                if j < 0:
                                                    bifRBCsIndex3.append(i)
                                                else:
                                                    break
                                    noBifEvents3=len(bifRBCsIndex3)
                                else:
                                    bifRBCsIndex3=[]
                                    noBifEvents3=0
                                sign3=e3['sign']
                            else:
                                bifRBCsIndex3=[]
                                sign3=e3['sign']
                                noBifEvents3=0
                        else:
                            bifRBCsIndex3=[]
                            noBifEvents3=0
                        #If bifurcation Events are possible check how many overshoots there are at the inEdges
                        if oe['RBCinMax'] > 0:
                            overshootDist1=[e['rRBC'][bifRBCsIndex1]-[e['length']]*noBifEvents1 if sign == 1.0     
                                else [0]*noBifEvents1-e['rRBC'][bifRBCsIndex1]][0]
                            if sign != 1:
                                overshootDist1 = overshootDist1[::-1]
                            overshootTime1=np.array(overshootDist1 / ([e['v']]*noBifEvents1))
                            dummy1=np.array([1]*len(overshootTime1))
                            if noBifEvents2 > 0:
                                overshootDist2=[e2['rRBC'][bifRBCsIndex2]-[e2['length']]*noBifEvents2 if sign2 == 1.0
                                    else [0]*noBifEvents2-e2['rRBC'][bifRBCsIndex2]][0]
                                if sign2 != 0:
                                    overshootDist2 = overshootDist2[::-1]
                                overshootTime2=np.array(overshootDist2)/ np.array([e2['v']]*noBifEvents2)
                                dummy2=np.array([2]*len(overshootTime2))
                            else:
                                overshootDist2=[]
                                overshootTime2=[]
                                dummy2=[]
                            if len(inflowEdges) > 2:
                                if noBifEvents3 > 0:
                                    overshootDist3=[e3['rRBC'][bifRBCsIndex3]-[e3['length']]*noBifEvents3 if sign3 == 1.0
                                        else [0]*noBifEvents3-e3['rRBC'][bifRBCsIndex3]][0]
                                    if sign3 != 0:
                                        overshootDist3 = overshootDist3[::-1]
                                    overshootTime3=np.array(overshootDist3)/ np.array([e3['v']]*noBifEvents3)
                                    dummy3=np.array([3]*len(overshootTime3))
                                else:
                                    overshootDist3=[]
                                    overshootTime3=[]
                                    dummy3=[]
                            else:
                                overshootDist3=[]
                                overshootTime3=[]
                                dummy3=[]
                            overshootTimes=zip(np.concatenate([overshootTime1,overshootTime2,overshootTime3]), \
                                np.concatenate([dummy1,dummy2,dummy3]))
                            overshootTimes.sort()
                            overshootTime=[]
                            inEdge=[]
                            count1=0
                            count2=0
                            count3=0
                            if oe['RBCinMax'] > len(overshootTimes):
                                overshootsNo=int(len(overshootTimes))
                            else:
                                overshootsNo=int(oe['RBCinMax'])
                            for i in range(-1*overshootsNo,0):
                                overshootTime.append(overshootTimes[i][0])
                                inEdge.append(overshootTimes[i][1])
                                if overshootTimes[i][1] == 1:
                                    count1 += 1
                                elif overshootTimes[i][1] == 2:
                                    count2 += 1
                                elif overshootTimes[i][1] == 3:
                                    count3 += 1
                            position=np.array(overshootTime)*np.array([oe['v']]*overshootsNo)
                            position=self._check_new_RBC_position(oe,position,overshootsNo)
                            #Add rbcs to new Edge       
                            if oe['sign'] == 1.0:
                                oe['rRBC']=np.concatenate([position, oe['rRBC']])
                            else:
                                position = [oe['length']]*overshootsNo - position[::-1]
                                oe['rRBC']=np.concatenate([oe['rRBC'],position])
                            #Remove RBCs from inEdge1
                            if count1 > 0:
                                if sign == 1.0:
                                    e['rRBC']=e['rRBC'][:-count1]
                                else:
                                    e['rRBC']=e['rRBC'][count1::]
                            if noBifEvents2 > 0 and count2 > 0:
                                #Remove RBCs from old Edge 2
                                if sign2 == 1.0:
                                    e2['rRBC']=e2['rRBC'][:-count2]
                                else:
                                    e2['rRBC']=e2['rRBC'][count2::]
                            if len(inflowEdges) > 2:
                                if noBifEvents3 > 0 and count3 > 0:
                                    #Remove RBCs from old Edge 3
                                    if sign3 == 1.0:
                                        e3['rRBC']=e3['rRBC'][:-count3]
                                    else:
                                        e3['rRBC']=e3['rRBC'][count3::]
                        else:
                            count1=0
                            count2=0
                            count3=0
                        #Deal with RBCs which could not be reassigned to the new edge because of a traffic jam
                        #InEdge 1
                        noStuckRBCs1=len(bifRBCsIndex1)-count1
                        if noStuckRBCs1 >0:
                            e['rRBC']=self._reposition_stuck_RBCs(e,noStuckRBCs1)
                        #InEdge 2
                        noStuckRBCs2=len(bifRBCsIndex2)-count2
                        if noStuckRBCs2 >0:
                            e2['rRBC']=self._reposition_stuck_RBCs(e2,noStuckRBCs2)
                        if len(inflowEdges) > 2:
                            #InEdge 3
                            noStuckRBCs3=len(bifRBCsIndex3)-count3
                            if noStuckRBCs3 >0:
                                e3['rRBC']=self._reposition_stuck_RBCs(e3,noStuckRBCs3)
        #-------------------------------------------------------------------------------------------
                    #if vertex is double connecting vertex
                    elif G.vs[vi]['vType'] == 6:
                        print('it is a double connecting bifurcation')
                        bifRBCsIndex1=bifRBCsIndex
                        noBifEvents1=noBifEvents
                        outE=G.vs[vi]['outflowE'][0]
                        sign = e['sign']
                        inflowEdges=G.vs[vi]['inflowE']
                        for i in inflowEdges:
                            if i == e.index:
                                inE1=e.index
                            else:
                                    inE2=i
                        e2=G.es[inE2]
                        if inE2 not in convEdges2:
                            convEdges2.append(inE2)
                            #Check if httBC exists
                            boolHttEdge2 = 0
                            if e2['httBC'] is not None:
                                boolHttEdge2 = 1
                                rRBC = []
                                lrbc = e2['minDist']
                                htt = e2['httBC']
                                length = e2['length']
                                nMaxNew=e2['nMax']-len(e2['rRBC'])
                                if len(e2['rRBC']) > 0:
                                    #if cum_length > distToFirst:
                                    posFirst=e2['rRBC'][0] if e2['sign']==1.0 else e2['length']-e2['rRBC'][-1]
                                    cum_length = posFirst
                                else:
                                    cum_length = e2['posFirst_last'] + e2['v_last'] * dt
                                    posFirst = cum_length
                                while cum_length >= lrbc and nMaxNew > 0:
                                    if len(e2['keep_rbcs']) != 0:
                                        if posFirst - e2['keep_rbcs'][0] >= 0:
                                            rRBC.append(posFirst - e2['keep_rbcs'][0])
                                            nMaxNew += -1
                                            posFirst=posFirst - e2['keep_rbcs'][0]
                                            cum_length = posFirst
                                            e2['keep_rbcs']=[]
                                            e2['posFirst_last']=posFirst
                                            e2['v_last']=e['v']
                                        else:
                                            if len(e2['rRBC']) > 0:
                                                e2['posFirst_last'] = posFirst
                                                e2['v_last']=e2['v']
                                            else:
                                                e2['posFirst_last'] += e2['v_last'] * dt
                                            break
                                    else:
                                        #number of RBCs randomly chosen to average htt
                                        number=np.exp(self._mu+self._sigma*np.random.randn(1)[0])
                                        self._spacing.append(number)
                                        spacing = lrbc+lrbc*number
                                        if posFirst - spacing >= 0:
                                            rRBC.append(posFirst - spacing)
                                            nMaxNew += -1
                                            posFirst=posFirst - spacing
                                            cum_length = posFirst
                                            e2['posFirst_last']=posFirst
                                            e2['v_last']=e2['v']
                                        else:
                                            e2['keep_rbcs']=[spacing]
                                            e2['v_last']=e2['v']
                                            if len(rRBC) == 0:
                                                e2['posFirst_last']=posFirst
                                            else:
                                                e2['posFirst_last']=rRBC[-1]
                                            break
                                rRBC = np.array(rRBC)
                                if len(rRBC) >= 1.:
                                    if e2['sign'] == 1:
                                        e2['rRBC'] = np.concatenate([rRBC[::-1], e2['rRBC']])
                                        vertexUpdate.append(e2.target)
                                        vertexUpdate.append(e2.source)
                                        edgeUpdate.append(e2.index)
                                    else:
                                        e2['rRBC'] = np.concatenate([e2['rRBC'], length-rRBC])
                                        vertexUpdate.append(e2.target)
                                        vertexUpdate.append(e2.source)
                                        edgeUpdate.append(e2.index)
                            #If RBCs are present move all RBCs in inEdge2
                            if len(e2['rRBC']) > 0:
                                e2['rRBC'] = e2['rRBC'] + e2['v'] * dt * e2['sign']
                                bifRBCsIndex2=[]
                                nRBC2=len(e2['rRBC'])
                                if e2['sign'] == 1.0:
                                    if e2['rRBC'][-1] > e2['length']:
                                        for i,j in enumerate(e2['rRBC'][::-1]):
                                            if j > e2['length']:
                                                bifRBCsIndex2.append(nRBC2-1-i)
                                            else:
                                                break
                                    bifRBCsIndex2=bifRBCsIndex2[::-1]
                                else:
                                    if e2['rRBC'][0] < 0:
                                        for i,j in enumerate(e2['rRBC']):
                                            if j < 0:
                                                bifRBCsIndex2.append(i)
                                            else:
                                                break
                                noBifEvents2=len(bifRBCsIndex2)
                            else:
                                noBifEvents2=0
                                bifRBCsIndex2=[]
                        else:
                            bifRBCsIndex2=[]
                            noBifEvents2=0
                        sign2=e2['sign']
                        #Define outEdges
                        outEdges=G.vs[vi]['outflowE']
                        outE=outEdges[0]
                        outE2=outEdges[1]
		        #Differ between capillaries and non-capillaries
                        if G.vs[vi]['isCap']:
                            preferenceList = [x[1] for x in \
                                sorted(zip(np.array(G.es[outEdges]['flow'])/np.array(G.es[outEdges]['crosssection']), \
                                outEdges), reverse=True)]
                        else:
                            preferenceList = [x[1] for x in sorted(zip(G.es[outEdges]['flow'], outEdges), reverse=True)]
			#Define prefered OutEdges
                        outEPref=preferenceList[0]
                        outEPref2=preferenceList[1]
                        oe=G.es[outEPref]
                        oe2=G.es[outEPref2]
                        noBifEvents=noBifEvents1+noBifEvents2
			#Check bifurcation events taking place
                        if noBifEvents <= oe['RBCinMax']:
                            overshootsNo=noBifEvents
                            overshootsNo2=0
                        else:
                            overshootsNo=oe['RBCinMax']
                            if noBifEvents-overshootsNo < oe2['RBCinMax']:
                                overshootsNo2=noBifEvents-overshootsNo
                            else:
                                overshootsNo2 = oe2['RBCinMax']
                        overshootsNoTot=overshootsNo+overshootsNo2
                        if overshootsNo > 0:
                            overshootDist=[e['rRBC'][bifRBCsIndex1]-[e['length']]*noBifEvents1 if sign == 1.0     
                                else [0]*noBifEvents1-e['rRBC'][bifRBCsIndex1]][0]
                            if sign != 1:
                                overshootDist = overshootDist[::-1]
                            overshootTime1=overshootDist / ([e['v']]*noBifEvents1)
                            dummy1=np.array([1]*len(overshootTime1))
                            if noBifEvents2 > 0:
                                overshootDist2=[e2['rRBC'][bifRBCsIndex2]-[e2['length']]*noBifEvents2 if sign2 == 1.0
                                    else [0]*noBifEvents2-e2['rRBC'][bifRBCsIndex2]][0]
                                if sign2 != 0:
                                    overshootDist2 = overshootDist2[::-1]
                                overshootTime2=np.array(overshootDist2)/ np.array([e2['v']]*noBifEvents2)
                                dummy2=np.array([2]*len(overshootTime2))
                            else:
                                overshootDist2=[]
                                overshootTime2=[]
                                dummy2=[]
                            overshootTimes=zip(np.concatenate([overshootTime1,overshootTime2]),np.concatenate([dummy1,dummy2]))
                            overshootTimes.sort()
                            overshootTime=[]
                            inEdge=[]
                            count1=0
                            count2=0
                            for i in range(-1*overshootsNoTot,0):
                                overshootTime.append(overshootTimes[i][0])
                                inEdge.append(overshootTimes[i][1])
                                if overshootTimes[i][1] == 1:
                                    count1 += 1
                                else:
                                    count2 += 1
                            position=np.array(overshootTime[-1*overshootsNo::])*np.array([oe['v']]*overshootsNo)
                            position=self._check_new_RBC_position(oe,position,overshootsNo)
                            #Add rbcs to new Edge       
                            if oe['sign'] == 1.0:
                                oe['rRBC']=np.concatenate([position, oe['rRBC']])
                            else:
                                position = [oe['length']]*overshootsNo - position[::-1]
                                oe['rRBC']=np.concatenate([oe['rRBC'],position])
                            if overshootsNo2 > 0:
                                if overshootsNo == 0:
                                    position2=np.array(overshootTime[-1*(overshootsNo+overshootsNo2)::])\
                                        *np.array([oe2['v']]*overshootsNo2)
                                else:
                                    position2=np.array(overshootTime[-1*(overshootsNo+overshootsNo2): \
                                        -1*overshootsNo:])*np.array([oe2['v']]*overshootsNo2)
                                position2=self._check_new_RBC_position(oe2,position2,overshootsNo2)
                                #Add rbcs to new Edge       
                                if oe2['sign'] == 1.0:
                                    oe2['rRBC']=np.concatenate([position2, oe2['rRBC']])
                                else:
                                    position2 = [oe2['length']]*overshootsNo2 - position2[::-1]
                                    oe2['rRBC']=np.concatenate([oe2['rRBC'],position2])
                            #Remove RBCs from inEdge1
                            if count1 > 0:
                                if sign == 1.0:
                                    e['rRBC']=e['rRBC'][:-count1]
                                else:
                                    e['rRBC']=e['rRBC'][count1::]
                            if noBifEvents2 > 0 and count2 > 0:
                                #Remove RBCs from old Edge 2
                                if sign2 == 1.0:
                                    e2['rRBC']=e2['rRBC'][:-count2]
                                else:
                                    e2['rRBC']=e2['rRBC'][count2::]
                        else:
                            count1=0
                            count2=0
                            count3=0
                        #Deal with RBCs which could not be reassigned to the new edge because of a traffic jam
                        #InEdge 1
                        noStuckRBCs1=len(bifRBCsIndex1)-count1
                        if noStuckRBCs1 >0:
                            e['rRBC']=self._reposition_stuck_RBCs(e,noStuckRBCs1)
                        #InEdge 2
                        noStuckRBCs2=len(bifRBCsIndex2)-count2
                        if noStuckRBCs2 >0:
                            e2['rRBC']=self._reposition_stuck_RBCs(e2,noStuckRBCs2)
       #-------------------------------------------------------------------------------------------
                if overshootsNo != 0:
                    vertexUpdate.append(e.target)
                    vertexUpdate.append(e.source)
                    for i in edgesInvolved:
                        edgeUpdate.append(i)
                    if self._analyzeBifEvents:
                        if G.vs['vType'][vi] == 3 or G.vs['vType'][vi] == 5:
                            rbcsMovedPerEdge.append(overshootsNo)
                            edgesWithMovedRBCs.append(e.index)
                        elif G.vs['vType'][vi] == 6:
                            if count1 > 0:
                                rbcsMovedPerEdge.append(count1)
                                edgesWithMovedRBCs.append(e.index)
                            if count2 > 0:
                                edgesWithMovedRBCs.append(e2.index)
                                rbcsMovedPerEdge.append(count2)
                        elif G.vs['vType'][vi] == 4:
                            if count1 > 0:
                                rbcsMovedPerEdge.append(count1)
                                edgesWithMovedRBCs.append(e.index)
                            if count2 > 0:
                                edgesWithMovedRBCs.append(e2.index)
                                rbcsMovedPerEdge.append(count2)
                            if len(inflowEdges) > 2:
                                if count3 > 0:
                                    rbcsMovedPerEdge.append(count3)
                                    edgesWithMovedRBCs.append(e.index)
                print('Analyze at end')
                for i in edgesInvolved:
                    if len(G.es['rRBC'][i]) > 0:
                        for j in range(len(G.es['rRBC'][i])-1):
                            if G.es['rRBC'][i][j+1]-G.es['rRBC'][i][j] + eps < G.es['minDist'][i]:
                                print('BIG ERROR START')
                                print(G.es['rRBC'][i][j+1]-G.es['rRBC'][i][j])
                                print(G.es['minDist'][i])
                                print(G.es['rRBC'][i][j+1])
                                print(G.es['rRBC'][i][j])
                                print('Bifurcation Event Details')
                                print(overshootsNo)
                                print(vi)
                                print(G.vs['vType'][vi])
                    if len(G.es['rRBC'][i]) > 0:
                        if G.es['rRBC'][i][0] < 0:
                            print('BIGERROR')
                            print(G.es['rRBC'][i][0])
                            print(overshootsNo)
                            print(vi)
                            print(G.vs['vType'][vi])
                        if G.es['rRBC'][i][-1] > G.es['length'][i]:
                            print('BIGERROR 2')
                            print(G.es['rRBC'][i][-1])
                            print(G.es['length'][i])
                            print(overshootsNo)
                            print(vi)
                            print(G.vs['vType'][vi])

        self._edgeUpdate=np.unique(edgeUpdate)
        self._vertexUpdate=np.unique(vertexUpdate)
        G.es['nRBC'] = [len(e['rRBC']) for e in G.es]
        self._G=G
        if self._analyzeBifEvents:
            self._rbcMoveAll.append(rbcMoved)
            self._rbcsMovedPerEdge=rbcsMovedPerEdge
    #--------------------------------------------------------------------------
    def _check_new_RBC_position(self,outEdge,position,overshootsNo):
        #Check if the leadin RBC overshoots too far or if the leading RBC
        #overshooted the whole vessel
        minDist=outEdge['minDist']
        #calculate maximum position for leading RBC
        if len(outEdge['rRBC']) > 0:
            posMax=outEdge['rRBC'][0]-minDist if outEdge['sign'] == 1.0 \
                else outEdge['length']-outEdge['rRBC'][-1]-minDist
        else:
            posMax=outEdge['length']
        collision=0
        #check if first RBC moved to far, if yes push RBC backwards
        if position[-1] > posMax:
            collision=1
            collisionStart=-1 #position of RBC that has been pushed backwards
            position[-1]=posMax
        #Check if there is enough space between the RBCs
        if collision == 0:
            for i in range(-1,-1*overshootsNo,-1):
                if position[i]-position[i-1] < minDist:
                    collision=1
                    collisionStart=i
                    break
        #Move RBCs such that they do not overlap
        if collision:
            #Check if they all fit behind behind the leading RBC
            #Pushing back is possible
            for i in range(collisionStart,-1*overshootsNo,-1):
                if position[i]-position[i-1] < minDist \
                    or position[i-1] > position[i]:
                    position[i-1]=position[i] - minDist
            #TODO do we really want pusshing forward?
            #Pushing forward is necesary
            if position[0] < 0:
                position[0]=0
                for i in range(0,overshootsNo-1):
                    if position[i+1]-position[i] < minDist \
                        or position[i] > position[i+1]:
                        position[i+1]=position[i] + minDist
        return position

    #--------------------------------------------------------------------------
    def _reposition_stuck_RBCs(self,edge,noStuckRBCs):
        minDist=edge['minDist']
        length=edge['length']
        sign=edge['sign']
        #move stuck RBCs backwards
        for i in range(noStuckRBCs):
            index=-1*(i+1) if sign == 1.0 else i
            edge['rRBC'][index]=length-i*minDist if sign == 1.0 \
                else 0+i*minDist
        #Recheck if the distance between the newly introduced RBCs is still big enough 
        if edge['sign']==1.0:
            for i in range(-1*noStuckRBCs,-1*len(edge['rRBC']),-1):
                if edge['rRBC'][i]-edge['rRBC'][i-1] < minDist \
                    or edge['rRBC'][i-1] > edge['rRBC'][i]:
                    edge['rRBC'][i-1]=edge['rRBC'][i]-minDist
                else:
                    break
        else:
            for i in range(noStuckRBCs-1,len(edge['rRBC'])-1):
                if edge['rRBC'][i+1]-edge['rRBC'][i] < minDist \
                    or edge['rRBC'][i] > edge['rRBC'][i+1]:
                    edge['rRBC'][i+1]=edge['rRBC'][i]+minDist
                else:
                    break
        return edge['rRBC']
    #--------------------------------------------------------------------------
    #@profile
    def evolve(self, time, method, dtfix,**kwargs):
        """Solves the linear system A x = b using a direct or AMG solver.
        INPUT: time: The duration for which the flow should be evolved. In case of
	 	     Reset in plotPrms or samplePrms = False, time is the duration 
	 	     which is added
               method: Solution-method for solving the linear system. This can
                       be either 'direct' or 'iterative'
               dtfix: given timestep
               **kwargs
               precision: The accuracy to which the ls is to be solved. If not
                          supplied, machine accuracy will be used. (This only
                          applies to the iterative solver)
               plotPrms: Provides the parameters for plotting the RBC 
                         positions over time. List format with the following
                         content is expected: [start, stop, step, reset].
                         'reset' is a boolean which determines if the current 
                         RBC evolution should be added to the existing history
                         or started anew. In case of Reset=False, start and stop
			 are added to the already elapsed time.
               samplePrms: Provides the parameters for sampling, i.e. writing 
                           a series of data-snapshots to disk for later 
                           analysis. List format with the following content is
                           expected: [start, stop, step, reset]. 'reset' is a
                           boolean which determines if the data samples should
                           be added to the existing database or a new database
                           should be set up. In case of Reset=False, start and stop
                          are added to the already elapsed time.
               SampleDetailed:Boolean whether every step should be samplede(True) or
			      if the sampling is done by the given samplePrms(False)
         OUTPUT: None (files are written to disk)
        """
        G=self._G
        tPlot = self._tPlot # deepcopy, since type is float
        tSample = self._tSample # deepcopy, since type is float
        filenamelist = self._filenamelist
        self._dt=dtfix
        timelist = self._timelist
	#filenamelistAvg = self._filenamelistAvg
	timelistAvg = self._timelistAvg

        if 'init' in kwargs.keys():
            init=kwargs['init']
        else:
            init=self._init

        SampleDetailed=False
        if 'SampleDetailed' in kwargs.keys():
            SampleDetailed=kwargs['SampleDetailed']

        doSampling, doPlotting = [False, False]

        if 'plotPrms' in kwargs.keys():
            pStart, pStop, pStep = kwargs['plotPrms']
            doPlotting = True
            if init == True:
                tPlot = 0.0
                filenamelist = []
                timelist = []
            else:
                tPlot=G['iterFinalPlot']
                pStart = G['iterFinalPlot']+pStart+pStep
                pStop = G['iterFinalPlot']+pStop

        if 'samplePrms' in kwargs.keys():
            sStart, sStop, sStep = kwargs['samplePrms']
            doSampling = True
            if init == True:
                self._tSample = 0.0
                self._sampledict = {}
                #self._transitTimeDict = {}
                #filenamelistAvg = []
                timelistAvg = []
            else:
                self._tSample = G['iterFinalSample']
                sStart = G['iterFinalSample']+sStart+sStep
                sStop = G['iterFinalSample']+sStop

        t1 = ttime.time()
        if init:
            self._t = 0.0
            BackUpTStart=0.1*time
            #BackUpTStart=0.0005*time
            BackUpT=0.1*time
            #BackUpT=0.0005*time
            BackUpCounter=0
        else:
            self._t = G['dtFinal']
            self._tSample=G['iterFinalSample']
            BackUpT=0.1*time
            print('Simulation starts at')
            print(self._t)
            print('First BackUp should be done at')
            time = G['dtFinal']+time
            BackUpCounter=G['BackUpCounter']+1
            BackUpTStart=G['dtFinal']+BackUpT
            print(BackUpTStart)
            print('BackUp should be done every')
            print(BackUpT)

        #Convert 'pBC' ['mmHG'] to default Units
        for v in G.vs:
            if v['pBC'] != None:
                v['pBC']=v['pBC']*self._scaleToDef

        tSample = self._tSample
        start_timeTot=ttime.time()
        t=self._t
        iteration=0
        while True:
            if t >= time:
                break
            iteration += 1
            start_time=ttime.time()
            self._update_eff_resistance_and_LS(None, self._vertexUpdate, False)
            print('Matrix updated')
            self._solve(method, **kwargs)
            print('Matrix solved')
            self._G.vs['pressure'] = deepcopy(self._x)
            print('Pressure copied')
            self._update_flow_and_velocity()
            print('Flow updated')
            self._update_flow_sign()
            print('Flow sign updated')
            self._verify_mass_balance()
            print('Mass balance verified updated')
            self._update_out_and_inflows_for_vertices()
            print('In and outflows updated')
            stdout.flush()
            self._update_RBCinMax()
            print('updated RBCinMax')
            #TODO plotting
            #if doPlotting and tPlot >= pStart and tPlot <= pStop:
            #    filename = 'iter_'+str(int(round(tPlot)))+'.vtp'
                #filename = 'iter_'+('%.3f' % t)+'.vtp'
            #    filenamelist.append(filename)
            #    timelist.append(tPlot)
            #    self._plot_rbc(filename)
            #    pStart = tPlot + pStep
                #self._sample()
                #filename = 'sample_'+str(int(round(tPlot)))+'.vtp'
                #self._plot_sample_average(filename)
            if SampleDetailed:
                print('sample detailed')
                stdout.flush()
                self._t=t
                self._tSample=tSample
                self._sample()
                filenameDetailed ='G_iteration_'+str(iteration)+'.pkl'
                #Convert deaultUnits to ['mmHG']
                #for 'pBC' and 'pressure'
                for v in G.vs:
                    if v['pBC'] != None:
                        v['pBC']=v['pBC']/self._scaleToDef
                    v['pressure']=v['pressure']/self._scaleToDef
                vgm.write_pkl(G,filenameDetailed)
                #Convert 'pBC' ['mmHG'] to default Units
                for v in G.vs:
                    if v['pBC'] != None:
                        v['pBC']=v['pBC']*self._scaleToDef
                    v['pressure']=v['pressure']*self._scaleToDef
            else:
                if doSampling and tSample >= sStart and tSample <= sStop:
                    print('DO sampling')
                    stdout.flush()
                    self._t=t
                    self._tSample=tSample
                    print('start sampling')
                    stdout.flush()
                    self._sample()
                    sStart = tSample + sStep
                    print('sampling DONE')
                    if t > BackUpTStart:
                        print('BackUp should be done')
                        print(BackUpCounter)
                        stdout.flush()
                        G['dtFinal']=t
                        G['iterFinalSample']=tSample
                        G['BackUpCounter']=BackUpCounter
                        G['rbcsMovedPerEdge']=self._rbcsMovedPerEdge
                        G['rbcMovedAll']=self._rbcMoveAll
                        filename1='sampledict_BackUp_'+str(BackUpCounter)+'.pkl'
                        filename2='G_BackUp'+str(BackUpCounter)+'.pkl'
                        self._sample_average()
                        print(filename1)
                        print(filename2)
                        #Convert deaultUnits to 'pBC' ['mmHG']
                        for v in G.vs:
                            if v['pBC'] != None:
                                v['pBC']=v['pBC']/self._scaleToDef
                            v['pressure']=v['pressure']/self._scaleToDef
                        g_output.write_pkl(self._sampledict,filename1)
                        vgm.write_pkl(G,filename2)
                        self._sampledict = {}
                        self._sampledict['averagedCount']=G['averagedCount']
                        #Convert 'pBC' ['mmHG'] to default Units
                        for v in G.vs:
                            if v['pBC'] != None:
                                v['pBC']=v['pBC']*self._scaleToDef
                            v['pressure']=v['pressure']*self._scaleToDef
                        BackUpCounter += 1
                        BackUpTStart += BackUpT
                        print('BackUp Done')
            print('START RBC propagate')
            stdout.flush()
            self._propagate_rbc()
            print('RBCs propagated')
            #TODO print change function
            #self._update_hematocrit(self._edgeUpdate)
            self._update_hematocrit()
            print('Hematocrit updated')
            tPlot = tPlot + self._dt
            self._tPlot = tPlot
            tSample = tSample + self._dt
            self._tSample = tSample
            t = t + self._dt
            log.info(t)
            print('TIME')
            print(t)
            print("Execution Time Loop:")
            print(ttime.time()-start_time, "seconds")
            print(' ')
            print(' ')
            stdout.write("\r%f" % tPlot)
            stdout.flush()
        stdout.write("\rDone. t=%f        \n" % tPlot)
        log.info("Time taken: %.2f" % (ttime.time()-t1))
        print("Execution Time:")
        print(ttime.time()-start_timeTot, "seconds")

        self._update_eff_resistance_and_LS(None, None, False)
        self._solve(method, **kwargs)
        self._G.vs['pressure'] = deepcopy(self._x)
        print('Pressure copied')
        self._update_flow_and_velocity()
        self._update_flow_sign()
        self._verify_mass_balance()
        print('Mass balance verified updated')
        self._t=t
        self._tSample=tSample
        stdout.flush()

        G['dtFinal']=t
        G['rbcsMovedPerEdge']=self._rbcsMovedPerEdge
        #G['iterFinalPlot']=tPlot
        G['iterFinalSample']=tSample
        G['rbcMovedAll']=self._rbcMoveAll
        G['BackUpCounter']=BackUpCounter
        filename1='sampledict_BackUp_'+str(BackUpCounter)+'.pkl'
        filename2='G_BackUp'+str(BackUpCounter)+'.pkl'
        #if doPlotting:
        #    filename= 'iter_'+str(int(round(tPlot+1)))+'.vtp'
        #    filenamelist.append(filename)
        #    timelist.append(tPlot)
        #    self._plot_rbc(filename)
        #    g_output.write_pvd_time_series('sequence.pvd', 
        #                                   filenamelist, timelist)
        if doSampling:
            self._sample()
            #Convert deaultUnits to 'pBC' ['mmHG']
            for v in G.vs:
                if v['pBC'] != None:
                    v['pBC']=v['pBC']/self._scaleToDef
                v['pressure']=v['pressure']/self._scaleToDef
            self._sample_average()
            g_output.write_pkl(self._sampledict, 'sampledict.pkl')
            g_output.write_pkl(self._sampledict,filename1)
	    #g_output.write_pkl(self._transitTimeDict, 'TransitTimeDict.pkl')
            #g_output.write_pvd_time_series('sequenceSampling.pvd',
	    #				   filenamelistAvg, timelistAvg)
        vgm.write_pkl(G, 'G_final.pkl')
        vgm.write_pkl(G,filename2)
        # Since Physiology has been rewritten using Cython, it cannot be
        # pickled. This class holds a Physiology object as a member and
        # consequently connot be pickled either.
        #g_output.write_pkl(self, 'LSHTD.pkl')
        #self._timelist = timelist[:]
        #self._filenamelist = filenamelist[:]
	#self._filenamelistAvg = filenamelistAvg[:]
	#self._timelistAvg = timelistAvg[:]

    #--------------------------------------------------------------------------

    def _plot_rbc(self, filename, tortuous=False):
        """Plots the current RBC distribution to vtp format.
        INPUT: filename: The name of the output file. This should have a .vtp
                         extension in order to be recognized by Paraview.
               tortuous: Whether or not to trace the tortuous path of the 
                         vessels. If false, linear tubes are assumed.
        OUTPUT: None, file written to disk.
        """
        G = self._G
        pgraph = vascularGraph.VascularGraph(0)
        r = []
        if tortuous:
            for e in G.es:
                if len(e['rRBC']) == 0:
                    continue
                p = e['points']
                cumlength = np.cumsum([np.linalg.norm(p[i] - p[i+1]) 
                                       for i in xrange(len(p[:-1]))])
                for rRBC in e['rRBC']:
                    i = np.nonzero(cumlength > rRBC)[0][0]
                    r.append(p[i-1] + (p[i] - p[i-1]) * 
                             (rRBC - cumlength[i-1]) / 
                             (cumlength[i] - cumlength[i-1]))
        else:
            for e in G.es:
                #points = e['points']
                #nPoints = len(points)
                rsource = G.vs[e.source]['r']
                dvec = G.vs[e.target]['r'] - G.vs[e.source]['r']
                length = e['length']
                for rRBC in e['rRBC']:
                    #index = int(round(npoints * rRBC / length))
                    r.append(rsource + dvec * rRBC/length)

	if len(r) > 0:
            pgraph.add_vertices(len(r))
            pgraph.vs['r'] = r
            g_output.write_vtp(pgraph, filename, False)
        else:
	    print('Network is empty - no plotting')

    #--------------------------------------------------------------------------
    
    def _sample(self):
        """Takes a snapshot of relevant current data and adds it to the sample
        database.
        INPUT: None
        OUTPUT: None, data added to self._sampledict
        """
        sampledict = self._sampledict
        G = self._G
        invivo = self._invivo
        
        htt2htd = self._P.tube_to_discharge_hematocrit
        du = self._G['defaultUnits']
        scaleToDef=self._scaleToDef
        #Convert default units to ['mmHG']
        pressure=np.array([1/scaleToDef]*G.vcount())*G.vs['pressure']

        for eprop in ['flow', 'v', 'htt', 'htd','nRBC','effResistance']:
            if not eprop in sampledict.keys():
                sampledict[eprop] = []
            sampledict[eprop].append(G.es[eprop])
        for vprop in ['pressure']:
            if not vprop in sampledict.keys():
                sampledict[vprop] = []
            sampledict[vprop].append(pressure)
        if not 'time' in sampledict.keys():
            sampledict['time'] = []
        sampledict['time'].append(self._tSample)
       

    #--------------------------------------------------------------------------

    def _plot_sample_average(self, sampleAvgFilename):
        """Averages the self._sampleDict data and writes it to disc.
        INPUT: sampleAvgFilename: Name of the sample average out-file.
        OUTPUT: None
        """
        sampledict = self._sampledict
        G = self._G
        if 'averagedCount' in sampledict.keys():
            avCount=sampledict['averagedCount']
        else:
            avCount = 0
        avCountNew=len(sampledict['time'])
        avCountE=np.array([avCount]*G.ecount())
        avCountNewE=np.array([avCountNew]*G.ecount())
        for eprop in ['flow', 'v', 'htt', 'htd','nRBC','effResistance']:
            if eprop+'_avg' in G.es.attribute_names():
                G.es[eprop + '_avg'] = (avCountE*G.es[eprop+'_avg']+ \
                    avCountNewE*np.average(sampledict[eprop], axis=0))/(avCountE+avCountNewE)
            else:
                G.es[eprop + '_avg'] = np.average(sampledict[eprop], axis=0)
            #if not [eprop + '_avg'] in sampledict.keys():
            #    sampledict[eprop + '_avg']=[]
            sampledict[eprop + '_avg']=G.es[eprop + '_avg']
        avCountV=np.array([avCount]*G.vcount())
        avCountNewV=np.array([avCountNew]*G.vcount())
        for vprop in ['pressure']:
            if vprop+'_avg' in G.vs.attribute_names():
                G.vs[vprop + '_avg'] = (avCountV*G.vs[vprop+'_avg']+ \
                    avCountNewV*np.average(sampledict[vprop], axis=0))/(avCountV+avCountNewV)
            else:
                G.vs[vprop + '_avg'] = np.average(sampledict[vprop], axis=0)
            #if not [vprop + '_avg'] in sampledict.keys():
            #    sampledict[vprop + '_avg']=[]
            sampledict[vprop + '_avg']=G.vs[vprop + '_avg']
        sampledict['averagedCount']=avCount + avCountNew
        G['averagedCount']=avCount + avCountNew

    #--------------------------------------------------------------------------

    def _sample_average(self):
        """Averages the self._sampleDict data and writes it to disc.
        INPUT: sampleAvgFilename: Name of the sample average out-file.
        OUTPUT: None
        """
        sampledict = self._sampledict
        G = self._G
        if 'averagedCount' in sampledict.keys():
            avCount=sampledict['averagedCount']
        else:
            avCount = 0
        avCountNew=len(sampledict['time'])
        avCountE=np.array([avCount]*G.ecount())
        avCountNewE=np.array([avCountNew]*G.ecount())
        for eprop in ['flow', 'v', 'htt', 'htd','nRBC','effResistance']:
            if eprop+'_avg' in G.es.attribute_names():
                G.es[eprop + '_avg'] = (avCountE*G.es[eprop+'_avg']+ \
                    avCountNewE*np.average(sampledict[eprop], axis=0))/(avCountE+avCountNewE)
            else:
                G.es[eprop + '_avg'] = np.average(sampledict[eprop], axis=0)
            #if not [eprop + '_avg'] in sampledict.keys():
            #    sampledict[eprop + '_avg']=[]
            sampledict[eprop + '_avg']=G.es[eprop + '_avg']
        avCountV=np.array([avCount]*G.vcount())
        avCountNewV=np.array([avCountNew]*G.vcount())
        for vprop in ['pressure']:
            if vprop+'_avg' in G.vs.attribute_names():
                G.vs[vprop + '_avg'] = (avCountV*G.vs[vprop+'_avg']+ \
                    avCountNewV*np.average(sampledict[vprop], axis=0))/(avCountV+avCountNewV)
            else:
                G.vs[vprop + '_avg'] = np.average(sampledict[vprop], axis=0)
            #if not [vprop + '_avg'] in sampledict.keys():
            #    sampledict[vprop + '_avg']=[]
            sampledict[vprop + '_avg']=G.vs[vprop + '_avg']
        sampledict['averagedCount']=avCount + avCountNew
        G['averagedCount']=avCount + avCountNew
        #g_output.write_vtp(G, sampleAvgFilename, False)


    #--------------------------------------------------------------------------
    #@profile
    def _solve(self, method, **kwargs):
        """Solves the linear system A x = b using a direct or AMG solver.
        INPUT: method: This can be either 'direct' or 'iterative'
               **kwargs
               precision: The accuracy to which the ls is to be solved. If not
                          supplied, machine accuracy will be used. (This only
                          applies to the iterative solver)
        OUTPUT: None, self._x is updated.
        """
        A = self._A.tocsr()
        if method == 'direct':
            linalg.use_solver(useUmfpack=True)
            x = linalg.spsolve(A, self._b)
        elif method == 'iterative':
            if kwargs.has_key('precision'):
                eps = kwargs['precision']
            else:
                eps = finfo(float).eps
            #AA = ruge_stuben_solver(A)
            AA = smoothed_aggregation_solver(A, max_levels=10, max_coarse=500)
            #PC = AA.aspreconditioner(cycle='V')
            #x,info = linalg.cg(A, self._b, tol=eps, maxiter=30, M=PC)
            #(x,flag) = pyamg.krylov.fgmres(A,self._b, maxiter=30, tol=eps)
            x = abs(AA.solve(self._b, tol=eps, accel='cg')) # abs required, as (small) negative pressures may arise
        elif method == 'iterative2':
         # Set linear solver
             ml = rootnode_solver(A, smooth=('energy', {'degree':2}), strength='evolution' )
             M = ml.aspreconditioner(cycle='V')
             # Solve pressure system
             #x,info = gmres(A, self._b, tol=self._eps, maxiter=50, M=M, x0=self._x)
             #x,info = gmres(A, self._b, tol=self._eps/10000000000000, maxiter=50, M=M)
             x,info = gmres(A, self._b, tol=self._eps/10000000000000, maxiter=50, M=M)
             if info != 0:
                 print('SOLVEERROR in Solving the Matrix')
             test = A * x
             res = np.array(test)-np.array(self._b)
        self._x = x
        ##self._res=res

    #--------------------------------------------------------------------------

    def _verify_mass_balance(self):
        """Computes the mass balance, i.e. sum of flows at each node and adds
        the result as a vertex property 'flowSum'.
        INPUT: None
        OUTPUT: None (result added as vertex property)
        """
        G = self._G
        G.vs['flowSum'] = [sum([G.es[e]['flow'] * np.sign(G.vs[v]['pressure'] -
                                                    G.vs[n]['pressure'])
                               for e, n in zip(G.adjacent(v), G.neighbors(v))])
                           for v in xrange(G.vcount())]
        for i in range(G.vcount()):
            if G.vs[i]['flowSum'] > 1e-4 and i not in G['av'] and i not in G['vv']:
                print('')
                print(i)
                print(G.vs['flowSum'][i])
                #print(self._res[i])
                print('FLOWERROR')
                for j in G.adjacent(i):
                    print(G.es['flow'][j])


    #--------------------------------------------------------------------------

    def _verify_rbc_balance(self):
        """Computes the rbc balance, i.e. sum of rbc flows at each node and
        adds the result as a vertex property 'rbcFlowSum'.
        INPUT: None
        OUTPUT: None (result added as vertex property)
        """
        G = self._G
        vf = self._P.velocity_factor
        invivo=self._invivo
        lrbc = self._P.effective_rbc_length
        tubeHt = [0.0 if e['tubeHt'] is None else e['tubeHt'] for e in G.es]
        G.vs['rbcFlowSum'] = [sum([4.0 * G.es[e]['flow'] * vf(G.es[e]['diameter'],invivo) * tubeHt[e] /
                                   np.pi / G.es[e]['diameter']**2 / lrbc(G.es[e]['diameter']) *
                                   np.sign(G.vs[v]['pressure'] - G.vs[n]['pressure'])
                                   for e, n in zip(G.adjacent(v), G.neighbors(v))])
                              for v in xrange(G.vcount())]

    #--------------------------------------------------------------------------

    def _verify_p_consistency(self):
        """Checks for local pressure maxima at non-pBC vertices.
        INPUT: None.
        OUTPUT: A list of local pressure maxima vertices and the maximum 
                pressure difference to their respective neighbors."""
        G = self._G
        localMaxima = []
        #Convert 'pBC' ['mmHG'] to default Units
        for v in G.vs:
            if v['pBC'] != None:
                v['pBC']=v['pBC']*self._scaleToDef

        for i, v in enumerate(G.vs):
            if v['pBC'] is None:
                pdiff = [v['pressure'] - n['pressure']
                         for n in G.vs[G.neighbors(i)]]
                if min(pdiff) > 0:
                    localMaxima.append((i, max(pdiff)))         
        #Convert defaultUnits to 'pBC' ['mmHG']
        for v in G.vs:
            if v['pBC'] != None:
                v['pBC']=v['pBC']/self._scaleToDef

        return localMaxima

    #--------------------------------------------------------------------------
    
    def _residual_norm(self):
        """Computes the norm of the current residual.
        """
        return np.linalg.norm(self._A * self._x - self._b)
