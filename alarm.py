#!/usr/bin/env python
from enum import Enum
import paho.mqtt.client as mqtt  #import the client1
import time

import signal   #to detect CTRL C
import sys

import json #to generate payloads for mqtt publishing
import random #for the disarm pin
import threading    #for timing the countdowns

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

class MqttAlarm( object ):
    """ An alarm that publishes its status using mqtt and receives commands the same way
    """

    #these are the only commands that may be sent over mqtt

    def __init__( self, armingCountdown, triggeredCountdown, disarmPin, mqttId, mqttParams, status = Status( StatusMain.UNARMED, 0 ) ):
        self.status = status
        self.countdown = 0
        self.mqttParams = mqttParams
        self.armingCountdown = armingCountdown
        self.triggeredCountdown = triggeredCountdown
        self.disarmPin = disarmPin
        self.modPin = ''

        #create a mqtt client
        self.client = mqtt.Client( mqttId )
        self.client.on_connect = self.__on_connect
        self.client.on_message = self.__on_message
        self.client.connect( self.mqttParams.address, self.mqttParams.port )
        self.client.loop_start()
        signal.signal( signal.SIGINT, self.__signalHandler )
        
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
        print( 'Received message {}'.format( text ) )
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
            print( 'Unknown command: [{0}]'.formart( text ) )
        

    def __arm( self, finalArmStatus ):
        """ Sets the status to arming and calls __doArm with the finalStatus
            to be set after countdown
        """
        self.__setStatus( Status( StatusMain.ARMING, self.armingCountdown ) );
        self.__doArm( finalArmStatus )
    
    def __doArm( self, finalArmStatus ):
        """ Will check countdown. If not 0, decrement and reschedule execution.
            Else set finalArmingStatus
        """
        if( StatusMain.ARMING != self.status.main ):
            print( '__doArm will stop here because status is {} instead of ARMING'.format( self.status.main ) )
            return

        if( self.status.countdown == 0 ):
            print( '__doArm will set status to {} because countdown has finished'.format( finalArmStatus.main ) )
            self.__setStatus( finalArmStatus )
        else:
            threading.Timer(1.0, self.__doArm, [ finalArmStatus ] ).start()
            print( '__doArm countdown {} to get to {}'.format( self.status.countdown, finalArmStatus.main ) )
            self.__setStatus( Status( StatusMain.ARMING, self.status.countdown -1 ) )

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
        print( 'Attempt to deactivate with text: "{}" and challengePin: "{}"'.format( text, self.status.challengePin ) )
        response = json.loads( text )
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
            self.__setStatus( Status( StatusMain.UNARMED ) )

    def __setStatus( self, status ):
        self.status = status
        self.client.publish( self.mqttParams.publishTopic, self.status.toJson() )

if( __name__ == '__main__' ):
    alarm = MqttAlarm( 15, 25, '9497', 'Alarm', MqttParams( "192.168.1.79", "1883", "A/4/A/set", "A/4/A/status" ) )
    
'''
def on_connect(client, userdata, flags, rc):
    m = "Connected flags"+str(flags)+"result code " + str(rc)+"client1_id  "+str(client)
    print(m)

def on_message(client1, userdata, message):
    print("message received  "  ,str(message.payload.decode("utf-8")))

broker_address="192.168.1.79"
broker_port="1883"

client1 = mqtt.Client("Alarm")    #create new instance
client1.on_connect= on_connect        #attach function to callback
client1.on_message=on_message        #attach function to callback
time.sleep(1)
client1.connect( broker_address, broker_port )      #connect to broker
client1.loop_start()    #start the loop
client1.subscribe( "A/4/A/set" )
client1.publish( "A/4/A/status", '{"main": "UNARMED", "countdown": 0 }' )
for i in range( 25, 0, -1 ):
    client1.publish( "A/4/A/status", '{"main": "ARMING", "countdown": ' + str(i) + ' }' ) #json string must be enclosed in double quotes
    time.sleep(1)
client1.publish( "A/4/A/status", '{"main": "ARMED_AWAY", "countdown": 0 }' )
client1.disconnect()
client1.loop_stop()
'''