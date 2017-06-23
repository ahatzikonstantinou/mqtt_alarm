#!/usr/bin/env python
from enum import Enum
import paho.mqtt.client as mqtt  #import the client1
from datetime import datetime
import time

import signal   #to detect CTRL C
import sys

import json #to generate payloads for mqtt publishing
import random #for the disarm pin
import threading    #for timing the countdowns
import os.path # to check if configuration file exists
import re # regular expression to detect trigger events

from pprint import pprint #for debug printing

class MqttParams( object ):
    """ Holds the mqtt connection params
    """
    def __init__( self, address, port, subscribeTopic, publishTopic ):
        self.address = address
        self.port = port
        self.subscribeTopic = subscribeTopic
        self.publishTopic = publishTopic

class StatusMain( Enum ):
    """The possible status values for the alarm status main property

    Values:
        UNARMED:    self explanatory
        ARMING:     counting down until it becomes ARMED
        ARMED_HOME: armed with people in the house/pace (usefull in order to not monitor certain events such as lights turning on)
        ARMED_AWAY: armed in an empty house/place(i.e. monitor extra events such as lights turing on, etc.)
        TRIGGERED:  an event has triggered the alarm and is counting down until it gets ACTIVATED
        ACTIVATED:  sirens screaming, lights flashing, sending sms/email/IM messages, making alert phone calls etc
    """
    UNARMED = 1
    ARMING = 2
    ARMED_HOME = 3
    ARMED_AWAY = 4
    TRIGGERED = 5
    ACTIVATED = 6

class Status( object ):
    """ The status of an alarm

    Attributes:
        main: the actual status eumeration
        countdown: the countdown value when ARMING  or TRIGGERED
        challengePin: The disarm message must contain the correct pin answer i.e. (challengePin + disarmPin)%10
    """
    def __init__( self, main = StatusMain.UNARMED, countdown = 0, challengePin = '' ):
        self.main = main
        self.countdown = countdown
        self.challengePin = challengePin

    def toJson( self ):
        """ Json strings must enclose tokens and string literals in double quotes
        """
        return json.dumps( { 'main': self.main.name, 'countdown': self.countdown, 'challengePin': self.challengePin } )

    def __eq__( self, other ):
        if isinstance(other, self.__class__):
            return self.__dict__ == other.__dict__
        return False

    def __ne__( self, other ):
        """Define a non-equality test
        """
        return not self.__eq__(other)

class AlarmCommand( Enum ):
    """ The commands that the alarm accepts over mqtt
    """
    ARM_HOME = 1
    ARM_AWAY = 2
    DEACTIVATE_REQUEST = 3  #this is the first step when disarming a triggered alarm. The alarm responds with a 10-base moduled pin and then the DISARM command must contain the correct answer
    DEACTIVATE = 4
    DISARM = 5

class Trigger( object ):
    """ Definition of how to detect trigger events. When armed the alarm will subscribe to certain mqtt topics..
        When such a message arrives and the corresponding regex matches, the alarm will trigger
    
    Attributes:
        topics: list of topics to subscibe and listen for incoming messages
        regex: a regeular expression to be matched against the payload of the received message. If matched the alarm triggers
    """
    def __init__( self, topics, regex ):
        self.topics = topics
        self.regex = regex

class MqttAlarm( object ):
    """ An alarm that publishes its status using mqtt and receives commands the same way
    """

    #these are the only commands that may be sent over mqtt

    def __init__( self, armingCountdown, triggeredCountdown, disarmPin, mqttId, mqttParams, triggersArmedAway, triggersArmedHome, status = Status( StatusMain.UNARMED, 0 ) ):
        self.status = status
        self.countdown = 0
        self.mqttParams = mqttParams
        self.armingCountdown = armingCountdown
        self.triggeredCountdown = triggeredCountdown
        self.mqttId = mqttId
        self.disarmPin = disarmPin
        self.modPin = ''
        self.triggersArmedAway = triggersArmedAway
        self.triggersArmedHome = triggersArmedHome
        self.armStatus = None   # need to store this in case alarm status cahnges to TRIGGERED or ACTIVATED

        signal.signal( signal.SIGINT, self.__signalHandler )
        
    def run( self ):
        #create a mqtt client
        self.client = mqtt.Client( self.mqttId )
        self.client.on_connect = self.__on_connect
        self.client.on_message = self.__on_message
        self.client.connect( self.mqttParams.address, self.mqttParams.port )
        self.client.loop_start()
        #go in infinite loop
        while( True ):
            pass

    def __signalHandler( self, signal, frame ):
        print('Ctrl+C pressed!')
        self.client.disconnect()
        self.client.loop_stop()
        sys.exit(0)        

    def __on_connect( self, client, userdata, flags_dict, result ):
        """Executed when a connection with the mqtt broker has been established
        """
        #debug:
        m = "Connected flags"+str(flags_dict)+"result code " + str(result)+"client1_id  "+str(client)
        print( m )

        #subscribe to start listening for incomming commands
        self.client.subscribe( self.mqttParams.subscribeTopic )

        #publish the initial status
        self.client.publish( self.mqttParams.publishTopic, self.status.toJson() )

    def __on_message( self, client, userdata, message ):
        """Executed when an mqtt arrives
        """
        text = message.payload.decode( "utf-8" )
        print( 'Received message "{}"'.format( text ) )
        if( mqtt.topic_matches_sub( self.mqttParams.subscribeTopic, message.topic ) ):
            if( text.find( AlarmCommand.ARM_HOME.name ) > -1 ):
                self.__arm( Status( StatusMain.ARMED_HOME ) )
            elif( text.find( AlarmCommand.ARM_AWAY.name ) > -1 ):
                self.__arm( Status( StatusMain.ARMED_AWAY ) )
            elif( text.find( AlarmCommand.DEACTIVATE_REQUEST.name ) > -1 ):
                self.__deactivateRequest()
            elif( text.find( AlarmCommand.DEACTIVATE.name ) > -1 ):
                self.__deactivate( text )
            elif( text.find( AlarmCommand.DISARM.name ) > -1 ):
                self.__disarm()
            else:
                print( 'Unknown command: [{0}]'.format( text ) )
        else:
            # check for trigger when alarm in one of specific statuses
            if( self.status.main in [ StatusMain.ARMED_AWAY, StatusMain.ARMED_HOME ] ):
                triggers = []
                if( StatusMain.ARMED_AWAY == self.armStatus ):
                    triggers = self.triggersArmedAway
                elif( StatusMain.ARMED_HOME == self.armStatus ):
                    triggers = self.triggersArmedHome
                for t in triggers:
                    for o in t.topics: 
                        if( mqtt.topic_matches_sub( o, message.topic ) and re.search( t.regex, text ) is not None ):
                            self.__logTrigger( message )
                            self.__trigger( message )                            
            elif( self.status.main in [ StatusMain.ACTIVATED, StatusMain.TRIGGERED ] ):
                #when already activated or triggered just log the trigger event
                self.__logTrigger( message )

    def __logActivation( self, message ):
        print( 'Alarm was ACTIVATED at [{}] by message <{}> "{}"'.format( datetime.now(), message.topic, message.payload.decode( "utf-8" ) ) )
    
    def __activate( self, message ):
        self.__logActivation( message )
        self.__setStatus( Status( StatusMain.ACTIVATED ) )

    def __logTrigger( self, message ):
        print( 'Alarm was triggered at [{}] by message <{}> "{}"'.format( datetime.now(), message.topic, message.payload.decode( "utf-8" ) ) )

    def __trigger( self, message ):
        if( self.status.main not in [ StatusMain.ARMED_AWAY, StatusMain.ARMED_HOME ] ):
            return
        print( 'Triggered!' )
        self.__setStatus( Status( StatusMain.TRIGGERED, self.triggeredCountdown ) )
        self.__doTrigger( message )

    def __doTrigger( self, message ):
        """Will activate when triggerCountdown is finished
        """
        if( StatusMain.TRIGGERED != self.status.main ):
            print( '__trigger will stop here because status is {} instead of ACTIVATED. The alarm was probably deactivated'.format( self.status.main ) )
            return
        
        if( self.status.countdown > 0 ):
            threading.Timer(1.0, self.__doTrigger, [message] ).start()
            print( '__doTrigger countdown {} to get to TRIGGERED'.format( self.status.countdown ) )
            self.__setStatus( Status( StatusMain.TRIGGERED, self.status.countdown -1 ) )
        else:
            print( '__doTrigger will set alarm to ACTIVATED because countdown has finished' )
            self.__activate( message )
    
    def __arm( self, finalArmStatus ):
        """ Sets the status to arming and calls __doArm with the finalStatus
            to be set after countdown
        """
        if( StatusMain.UNARMED != self.status.main ):
            return

        self.__setStatus( Status( StatusMain.ARMING, self.armingCountdown ) );
        self.__doArm( finalArmStatus )
    
    def __doArm( self, finalArmStatus ):
        """ Will check countdown. If not 0, decrement and reschedule execution.
            Else set finalArmingStatus
        """
        if( StatusMain.ARMING != self.status.main ):
            print( '__doArm will stop here because status is {} instead of ARMING'.format( self.status.main ) )
            return

        if( self.status.countdown > 0 ):
            threading.Timer(1.0, self.__doArm, [ finalArmStatus ] ).start()
            print( '__doArm countdown {} to get to {}'.format( self.status.countdown, finalArmStatus.main ) )
            self.__setStatus( Status( StatusMain.ARMING, self.status.countdown -1 ) )
        else:
            print( '__doArm will set status to {} because countdown has finished'.format( finalArmStatus.main ) )

            #subscribe to trigger topics
            triggers = []
            if( StatusMain.ARMED_AWAY == finalArmStatus.main ):
                triggers = self.triggersArmedAway
            elif( StatusMain.ARMED_HOME == finalArmStatus.main ):
                triggers = self.triggersArmedHome
            for t in triggers:
                for o in t.topics:                    
                    print( '\tsubscribing to trigger topic: {}', o )
                    self.client.subscribe( o )  #subscribe to trigger topics

            # set and publish new status
            self.armStatus = finalArmStatus.main
            self.__setStatus( finalArmStatus ) 

    def __deactivateRequest( self ):
        """ Generates a pin and mods with the disarmPin. Sends the result and expects the answer
        """
        sendPin = ''
        self.modPin = ''
        for i in range( 4 ):
            r = random.randint( 0, 9 )
            self.modPin += str( r )
            sendPin += str( ( r - int( self.disarmPin[i] ) ) % 10 )
        print( 'disarmPin: {}, modPin:{}, challengePin:{}'.format( self.disarmPin, self.modPin, sendPin ) )
        self.__setStatus( Status( self.status.main, self.status.countdown, sendPin ) )

    def __deactivate( self, text ):
        print( 'Attempt to deactivate with text: "{}"'.format( text ) )
        if( self.status.main not in [ StatusMain.TRIGGERED, StatusMain.ACTIVATED ] ):
            print( 'Current status {}. Will not attempt deactivation.'.format( self.status.main.name ) )
            return
        if( self.modPin is None or len( self.modPin ) == 0 ):
            print( 'self.modPin is empty. The DEACTIVATE cmd has probably not been preceded by a DEACTIVATE_REQUEST. Will not attempt deactivation.')
            return
            
        response = None
        try:
            response = json.loads( text )
        except ValueError, e:
            print( '"{}" is not a valid json text, exiting.'.format( text ) )
            return
        
        if( 'pin' not in response ):
            print( 'pin not found in text {}. Will not deactivate'.format( text ) )
            return
        pin = response['pin']
        for i in range( 4 ):            
            if( not pin[i].isdigit() ):
                print( '{} is not a digit, deactivate exits'.format( text[i] ) )
                return

            if( pin[i] != self.modPin[i] ):
                print( 'Wrong pin! Will not deactivate' )
                return
            
        print( 'pin:{} == modPin:{}. Will deactivate '.format( text, self.modPin ) )
        self.modPin = ''
        self.__setStatus( Status( StatusMain.UNARMED ) )

    def __disarm( self ):
        if( StatusMain.ARMED_AWAY == self.status.main or StatusMain.ARMED_HOME == self.status.main or StatusMain.ARMING == self.status.main ):
            print( 'Disarm will unsubscribe from trigger topics' )
            triggers = []
            if( StatusMain.ARMED_AWAY == self.status.main ):
                triggers = self.triggersArmedAway
            elif( StatusMain.ARMED_HOME == self.status.main ):
                triggers = self.triggersArmedHome
            for t in triggers:
                for o in t.topics:                    
                    print( '\tunsubscribing from trigger topic: {}', o )
                    self.client.unsubscribe( o )

            self.armStatus = StatusMain.UNARMED
            self.__setStatus( Status( StatusMain.UNARMED ) )

    def __setStatus( self, status ):
        self.status = status
        self.client.publish( self.mqttParams.publishTopic, self.status.toJson() )

if( __name__ == '__main__' ):
    configurationFile = 'alarm.conf'
    if( not os.path.isfile( configurationFile ) ):
        print( 'Configuration file "{}" not found, exiting.'.format( configurationFile ) )
        sys.exit()

    with open( configurationFile ) as json_file:
        configuration = json.load( json_file )
        print( 'Configuration: ' )
        pprint( configuration )

        alarm = MqttAlarm( 
            configuration['armingCountdown'], 
            configuration['triggeredCountdown'], 
            configuration['disarmPin'], 
            configuration['mqttId'], 
            MqttParams( configuration['mqttParams']['address'], int( configuration['mqttParams']['port'] ), configuration['mqttParams']['subscribeTopic'], configuration['mqttParams']['publishTopic'] ),
            [ Trigger( x['topics'], x['regex'] ) for x in configuration['triggers']['armedAway'] ],
            [ Trigger( x['topics'], x['regex'] ) for x in configuration['triggers']['armedHome'] ],
            Status( StatusMain[ configuration['status']['main'] ], configuration['status']['countdown'] )

        )
        alarm.run()